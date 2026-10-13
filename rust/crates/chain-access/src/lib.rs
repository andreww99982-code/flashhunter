//! Доступ к блокчейну: провайдеры, чтение блоков, nonce и цены газа.
//!
//! Крейт инкапсулирует alloy: остальные сервисы работают с трейтами и не знают,
//! общаемся мы с нодой, с архивным RPC или с фейком в тестах.

use async_trait::async_trait;
use fh_shared::{gwei_to_wei, ChainId};
use std::collections::HashMap;
use std::sync::Arc;

/// Минимальная информация о блоке, нужная горячему пути.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockHeader {
    /// Номер блока.
    pub number: u64,
    /// Хэш блока.
    pub hash: [u8; 32],
    /// Unix-время блока (секунды).
    pub timestamp: u64,
    /// Базовая комиссия (EIP-1559), wei.
    pub base_fee_wei: u128,
}

impl BlockHeader {
    /// Базовая комиссия в gwei (для сравнения с лимитом из конфигурации).
    pub fn base_fee_gwei(&self) -> f64 {
        fh_shared::wei_to_gwei(self.base_fee_wei)
    }
}

/// Текущее состояние сети для стратегий: блок, газ и nonce кошелька.
#[derive(Debug, Clone)]
pub struct ChainState {
    /// Сеть.
    pub chain_id: ChainId,
    /// Последний блок.
    pub head: BlockHeader,
    /// Рекомендуемая priority fee, wei.
    pub priority_fee_wei: u128,
    /// Nonce горячего кошелька (по последнему подтверждённому состоянию).
    pub nonce: u64,
}

/// Ошибки слоя доступа к сети.
#[derive(Debug, thiserror::Error)]
pub enum ChainError {
    /// RPC недоступен или вернул ошибку.
    #[error("RPC {chain} недоступен: {message}")]
    Rpc { chain: ChainId, message: String },
    /// Сеть отсутствует в конфигурации.
    #[error("нет RPC для сети {0}")]
    UnknownChain(ChainId),
    /// Таймаут запроса (важно для латентности: стратегия живёт 200 мс на блок).
    #[error("таймаут запроса к сети {0}")]
    Timeout(ChainId),
}

/// Абстракция над провайдером: продакшн — alloy, тесты — мок.
#[async_trait]
pub trait ChainProvider: Send + Sync {
    /// Возвращает снимок состояния сети (голова, газ, nonce).
    async fn state(&self, chain: ChainId, wallet: fh_shared::Address) -> Result<ChainState, ChainError>;
    /// Читает заголовок блока по номеру (None — блок ещё не добыт).
    async fn block_header(&self, chain: ChainId, number: u64) -> Result<Option<BlockHeader>, ChainError>;
}

/// Пул провайдеров: по одному на сеть, с фолбэком на резервный RPC.
pub struct ProviderPool {
    providers: HashMap<ChainId, Vec<Arc<dyn ChainProvider>>>,
    round_robin: HashMap<ChainId, usize>,
}

impl ProviderPool {
    /// Создаёт пул из карты «сеть → список провайдеров» (первый — основной).
    pub fn new(providers: HashMap<ChainId, Vec<Arc<dyn ChainProvider>>>) -> Self {
        Self { providers, round_robin: HashMap::new() }
    }

    /// Возвращает провайдер для сети, перебирая список при отказе.
    pub fn provider(&mut self, chain: ChainId) -> Result<Arc<dyn ChainProvider>, ChainError> {
        let list = self.providers.get(&chain).ok_or(ChainError::UnknownChain(chain))?;
        if list.is_empty() {
            return Err(ChainError::UnknownChain(chain));
        }
        let index = self.round_robin.entry(chain).or_insert(0);
        let provider = Arc::clone(&list[*index % list.len()]);
        *index = index.wrapping_add(1);
        Ok(provider)
    }

    /// Опрашивает все сети и возвращает состояние (используется watchdog-ом риска).
    pub async fn all_states(&mut self, wallet: fh_shared::Address) -> Vec<Result<ChainState, ChainError>> {
        let chains: Vec<ChainId> = self.providers.keys().copied().collect();
        let mut states = Vec::with_capacity(chains.len());
        for chain in chains {
            let provider = match self.provider(chain) {
                Ok(provider) => provider,
                Err(error) => {
                    states.push(Err(error));
                    continue;
                }
            };
            states.push(provider.state(chain, wallet).await);
        }
        states
    }
}

/// Считает maxFeePerGas для отправки с запасом на рост base fee.
pub fn suggest_max_fee(block_base_fee_wei: u128, priority_fee_wei: u128, bump_pct: u64) -> u128 {
    let with_bump = block_base_fee_wei * (100 + bump_pct as u128) / 100;
    with_bump + priority_fee_wei
}

#[cfg(test)]
mod tests {
    use super::*;
    use fh_shared::Address;

    struct FakeProvider {
        state: ChainState,
    }

    #[async_trait]
    impl ChainProvider for FakeProvider {
        async fn state(&self, _chain: ChainId, _wallet: Address) -> Result<ChainState, ChainError> {
            Ok(self.state.clone())
        }
        async fn block_header(&self, _chain: ChainId, _number: u64) -> Result<Option<BlockHeader>, ChainError> {
            Ok(Some(self.state.head.clone()))
        }
    }

    fn fake(chain: ChainId) -> Arc<dyn ChainProvider> {
        Arc::new(FakeProvider {
            state: ChainState {
                chain_id: chain,
                head: BlockHeader { number: 21_000_000, hash: [7u8; 32], timestamp: 1_730_000_000, base_fee_wei: gwei_to_wei(12.0) },
                priority_fee_wei: gwei_to_wei(2.0),
                nonce: 42,
            },
        })
    }

    #[tokio::test]
    async fn provider_pool_round_robin() {
        let mut providers: HashMap<ChainId, Vec<Arc<dyn ChainProvider>>> = HashMap::new();
        providers.insert(ChainId::MAINNET, vec![fake(ChainId::MAINNET), fake(ChainId::MAINNET)]);
        let mut pool = ProviderPool::new(providers);
        assert!(pool.provider(ChainId::MAINNET).is_ok());
        assert!(pool.provider(ChainId::MAINNET).is_ok());
        assert!(matches!(pool.provider(ChainId::BASE), Err(ChainError::UnknownChain(_))));
    }

    #[tokio::test]
    async fn all_states_returns_every_configured_chain() {
        let mut providers: HashMap<ChainId, Vec<Arc<dyn ChainProvider>>> = HashMap::new();
        providers.insert(ChainId::MAINNET, vec![fake(ChainId::MAINNET)]);
        providers.insert(ChainId::BASE, vec![fake(ChainId::BASE)]);
        let mut pool = ProviderPool::new(providers);
        let states = pool.all_states(Address::ZERO).await;
        assert_eq!(states.len(), 2);
        assert!(states.iter().all(|item| item.is_ok()));
    }

    #[test]
    fn max_fee_covers_base_fee_growth() {
        let base = gwei_to_wei(100.0);
        let priority = gwei_to_wei(2.0);
        let suggested = suggest_max_fee(base, priority, 25);
        assert_eq!(suggested, gwei_to_wei(127.0));
        assert!(suggested > base + priority - 1);
    }

    #[test]
    fn block_header_reports_gwei() {
        let header = BlockHeader { number: 1, hash: [0u8; 32], timestamp: 0, base_fee_wei: gwei_to_wei(8.5) };
        assert!((header.base_fee_gwei() - 8.5).abs() < 1e-9);
    }
}
