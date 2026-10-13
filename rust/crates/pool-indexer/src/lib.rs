//! Индексатор пулов: поддержка локального состояния пулов в памяти.
//!
//! Горячий путь MEV не может ходить в сеть за состоянием: к моменту нового блока
//! все пулы уже должны быть в памяти. Крейт хранит «горячий кэш» пулов и применяет
//! события Sync/Swap из логов, поддерживая инвариант консистентности.

use fh_shared::Address;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::time::{Duration, Instant};

/// Тип пула: определяет формулу расчёта выхода.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum PoolKind {
    /// Uniswap V2 / Sushi (постоянный продукт).
    ConstantProduct,
    /// Uniswap V3+ (концентрированная ликвидность, один активный диапазон).
    Concentrated,
    /// Curve (стабильный инвариант).
    StableSwap,
}

/// Состояние пула в горячем кэше.
#[derive(Debug, Clone)]
pub struct PoolState {
    /// Адрес пула.
    pub address: Address,
    /// Сеть.
    pub chain_id: fh_shared::ChainId,
    /// Тип пула.
    pub kind: PoolKind,
    /// Резерв токена 0 (raw).
    pub reserve0: u128,
    /// Резерв токена 1 (raw).
    pub reserve1: u128,
    /// Комиссия пула в bps (30 = 0.30%).
    pub fee_bps: u32,
    /// Ликвидность активного диапазона (только V3).
    pub liquidity: u128,
    /// Цена sqrt(P) в Q64.96 (только V3).
    pub sqrt_price_x96: u128,
    /// Номер блока последнего обновления.
    pub updated_block: u64,
    /// Момент обновления в памяти (для метрики «свежесть данных»).
    pub updated_at: Instant,
}

impl PoolState {
    /// Устарел ли пул: данные старше `max_age` считаются непригодными для торговли.
    pub fn is_stale(&self, max_age: Duration) -> bool {
        self.updated_at.elapsed() > max_age
    }

    /// Выход для V2-пула (целочисленно, как в контракте).
    pub fn constant_product_out(&self, amount_in: u128, zero_for_one: bool) -> u128 {
        let (reserve_in, reserve_out) = if zero_for_one {
            (self.reserve0, self.reserve1)
        } else {
            (self.reserve1, self.reserve0)
        };
        if reserve_in == 0 || reserve_out == 0 || amount_in == 0 {
            return 0;
        }
        let amount_with_fee = amount_in * (10_000 - self.fee_bps as u128);
        amount_with_fee * reserve_out / (reserve_in * 10_000 + amount_with_fee)
    }
}

/// Событие из логов, применяемое к состоянию пула.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PoolEvent {
    /// Синхронизация резервов (V2 Sync).
    Sync { address: Address, reserve0: u128, reserve1: u128 },
    /// Своп: обновление цены и ликвидности (V3 Swap).
    Swap { address: Address, sqrt_price_x96: u128, liquidity: u128 },
    /// Изменение ликвидности (Mint/Burn).
    LiquidityChange { address: Address, liquidity: u128 },
}

/// Ошибки индексатора.
#[derive(Debug, thiserror::Error)]
pub enum IndexerError {
    /// Пул не найден в кэше.
    #[error("пул {0} отсутствует в индексе")]
    UnknownPool(Address),
    /// Событие пришло из «прошлого» (реорганизация или опоздание): состояние не трогаем.
    #[error("устаревшее событие для пула {0}")]
    StaleEvent(Address),
}

/// Горячий индекс пулов: применяет события и отдаёт состояние стратегиям за O(1).
pub struct PoolIndexer {
    pools: HashMap<Address, PoolState>,
    /// С какого момента данные считаются устаревшими.
    pub max_age: Duration,
    /// Счётчик применённых событий (метрика для /metrics).
    pub applied_events: u64,
    /// Счётчик отброшенных устаревших событий.
    pub stale_events: u64,
}

impl Default for PoolIndexer {
    fn default() -> Self {
        Self::new(Duration::from_secs(12))
    }
}

impl PoolIndexer {
    /// Создаёт индекс с горизонтом устаревания данных.
    pub fn new(max_age: Duration) -> Self {
        Self { pools: HashMap::new(), max_age, applied_events: 0, stale_events: 0 }
    }

    /// Регистрирует пул (или обновляет его полное состояние из снапшота).
    pub fn upsert(&mut self, pool: PoolState) {
        self.pools.insert(pool.address, pool);
    }

    /// Возвращает состояние пула, если оно есть и не устарело.
    pub fn get_fresh(&self, address: &Address) -> Option<&PoolState> {
        self.pools.get(address).filter(|pool| !pool.is_stale(self.max_age))
    }

    /// Применяет событие из логов к состоянию пула.
    pub fn apply_event(&mut self, event: PoolEvent) -> Result<(), IndexerError> {
        let address = match &event {
            PoolEvent::Sync { address, .. }
            | PoolEvent::Swap { address, .. }
            | PoolEvent::LiquidityChange { address, .. } => *address,
        };
        let pool = self.pools.get_mut(&address).ok_or(IndexerError::UnknownPool(address))?;
        match event {
            PoolEvent::Sync { reserve0, reserve1, .. } => {
                pool.reserve0 = reserve0;
                pool.reserve1 = reserve1;
            }
            PoolEvent::Swap { sqrt_price_x96, liquidity, .. } => {
                pool.sqrt_price_x96 = sqrt_price_x96;
                pool.liquidity = liquidity;
            }
            PoolEvent::LiquidityChange { liquidity, .. } => {
                pool.liquidity = liquidity;
            }
        }
        pool.updated_at = Instant::now();
        self.applied_events += 1;
        Ok(())
    }

    /// Число пулов в индексе.
    pub fn len(&self) -> usize {
        self.pools.len()
    }

    /// Пуст ли индекс.
    pub fn is_empty(&self) -> bool {
        self.pools.is_empty()
    }

    /// Снимок пулов указанной сети (для передачи в sim-engine одним сообщением).
    pub fn snapshot(&self, chain_id: fh_shared::ChainId, limit: usize) -> Vec<PoolState> {
        let mut pools: Vec<PoolState> = self
            .pools
            .values()
            .filter(|pool| pool.chain_id == chain_id && !pool.is_stale(self.max_age))
            .cloned()
            .collect();
        pools.sort_by(|left, right| (right.reserve0 + right.reserve1).cmp(&(left.reserve0 + left.reserve1)));
        pools.truncate(limit);
        pools
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use fh_shared::{Address, ChainId};

    fn pool(reserve0: u128, reserve1: u128) -> PoolState {
        PoolState {
            address: Address([1u8; 20]),
            chain_id: ChainId::MAINNET,
            kind: PoolKind::ConstantProduct,
            reserve0,
            reserve1,
            fee_bps: 30,
            liquidity: 0,
            sqrt_price_x96: 0,
            updated_block: 1,
            updated_at: Instant::now(),
        }
    }

    #[test]
    fn upsert_and_get_fresh() {
        let mut indexer = PoolIndexer::default();
        indexer.upsert(pool(1_000_000, 1_000_000));
        assert!(indexer.get_fresh(&Address([1u8; 20])).is_some());
        assert_eq!(indexer.len(), 1);
    }

    #[test]
    fn stale_pool_is_filtered_out() {
        let mut indexer = PoolIndexer::new(Duration::from_millis(1));
        let mut state = pool(10, 10);
        state.updated_at = Instant::now() - Duration::from_secs(5);
        indexer.upsert(state);
        assert!(indexer.get_fresh(&Address([1u8; 20])).is_none());
        assert!(indexer.snapshot(ChainId::MAINNET, 10).is_empty());
    }

    #[test]
    fn event_updates_reserves_and_counter() {
        let mut indexer = PoolIndexer::default();
        indexer.upsert(pool(1_000, 1_000));
        indexer
            .apply_event(PoolEvent::Sync { address: Address([1u8; 20]), reserve0: 2_000, reserve1: 1_500 })
            .expect("событие должно примениться");
        let state = indexer.get_fresh(&Address([1u8; 20])).expect("пул в индексе");
        assert_eq!((state.reserve0, state.reserve1), (2_000, 1_500));
        assert_eq!(indexer.applied_events, 1);
    }

    #[test]
    fn event_for_unknown_pool_is_error() {
        let mut indexer = PoolIndexer::default();
        let result = indexer.apply_event(PoolEvent::Sync { address: Address([9u8; 20]), reserve0: 1, reserve1: 1 });
        assert!(matches!(result, Err(IndexerError::UnknownPool(_))));
    }

    #[test]
    fn constant_product_matches_v2_formula() {
        let state = pool(1_000_000_000, 1_000_000_000);
        let out = state.constant_product_out(1_000, true);
        let expected = 1_000u128 * (10_000 - 30) * 1_000_000_000 / (1_000_000_000 * 10_000 + 1_000 * (10_000 - 30));
        assert_eq!(out, expected);
        assert!(out < 1_000);
    }

    #[test]
    fn snapshot_sorted_by_depth_and_limited() {
        let mut indexer = PoolIndexer::default();
        let mut deep = pool(9_000_000, 9_000_000);
        deep.address = Address([2u8; 20]);
        indexer.upsert(pool(100, 100));
        indexer.upsert(deep);
        let snapshot = indexer.snapshot(ChainId::MAINNET, 1);
        assert_eq!(snapshot.len(), 1);
        assert_eq!(snapshot[0].address, Address([2u8; 20]));
    }
}
