//! Сборка bundle: транзакции, nonce, комиссии и хэш для отправки в релеи.
//!
//! Критично для MEV: bundle должен быть собран за единицы миллисекунд и
//! содержать корректный nonce. Ошибка в nonce = потерянный блок и убыток на газе.

use fh_shared::{gwei_to_wei, Address, ChainId};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// Транзакция внутри bundle (поля в camelCase — формат eth_sendBundle).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BundleTx {
    /// Куда отправляем (адрес контракта-исполнителя).
    pub to: Address,
    /// calldata (hex без 0x не храним — держим байты).
    pub data: Vec<u8>,
    /// Лимит газа.
    pub gas: u64,
    /// Отправляемое значение (wei), обычно 0.
    pub value: u128,
    /// Максимальная комиссия за газ.
    pub max_fee_per_gas: u128,
    /// Чаевые билдеру.
    pub max_priority_fee_per_gas: u128,
    /// Nonce отправителя.
    pub nonce: u64,
    /// Сеть.
    pub chain_id: u64,
}

impl BundleTx {
    /// Транзакция в формате JSON-RPC для eth_sendBundle.
    pub fn to_rpc(&self) -> Value {
        json!({
            "to": self.to.to_lower_hex(),
            "data": format!("0x{}", hex::encode(&self.data)),
            "gas": format!("0x{:x}", self.gas),
            "value": format!("0x{:x}", self.value),
            "maxFeePerGas": format!("0x{:x}", self.max_fee_per_gas),
            "maxPriorityFeePerGas": format!("0x{:x}", self.max_priority_fee_per_gas),
            "nonce": format!("0x{:x}", self.nonce),
            "chainId": format!("0x{:x}", self.chain_id),
            "type": "0x2",
        })
    }
}

/// План отправки: транзакции + служебные поля для журнала.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bundle {
    /// Сеть.
    pub chain_id: ChainId,
    /// Целевой контракт (для метрик и журнала).
    pub target: Address,
    /// Отправитель (горячий кошелёк).
    pub sender: Address,
    /// Nonce, использованный в плане.
    pub nonce: u64,
    /// Транзакции bundle.
    pub txs: Vec<BundleTx>,
    /// Целевой блок (None = следующий).
    pub target_block: Option<u64>,
}

impl Bundle {
    /// Хэш состава bundle (локальный аналог ответа релея — для журнала и матчинга).
    pub fn hash(&self) -> String {
        let mut hasher_input = format!("{}:{}:{}", self.chain_id, self.nonce, self.txs.len());
        for tx in &self.txs {
            hasher_input.push_str(&format!(":{:x}:{:x}:{}", tx.gas, tx.max_fee_per_gas, hex::encode(&tx.data)));
        }
        // SHA-256 реализован в крейте через alloy-primitives в прод-сборке;
        // здесь используем детерминированный FNV-1a 64 для нулевых зависимостей.
        let digest = fnv1a_64(hasher_input.as_bytes());
        format!("0x{digest:016x}{:016x}", fnv1a_64(&digest.to_le_bytes()))
    }

    /// Тело запроса eth_sendBundle.
    pub fn to_rpc_params(&self) -> Value {
        json!([{
            "txs": self.txs.iter().map(BundleTx::to_rpc).collect::<Vec<_>>(),
            "blockNumber": self.target_block.map(|block| format!("0x{block:x}")),
        }])
    }
}

/// Детерминированный 64-битный хэш (FNV-1a): не криптография, но воспроизводимо.
fn fnv1a_64(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in bytes {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x100_0000_01b3);
    }
    hash
}

/// Менеджер nonce: выдаёт последовательные значения и синхронизируется с сетью.
#[derive(Debug, Default)]
pub struct NonceManager {
    state: std::collections::HashMap<(ChainId, Address), u64>,
}

impl NonceManager {
    /// Новый менеджер.
    pub fn new() -> Self {
        Self::default()
    }

    /// Синхронизация с фактическим nonce из сети (перезаписывает локальный счётчик).
    pub fn sync(&mut self, chain: ChainId, sender: Address, nonce: u64) {
        self.state.insert((chain, sender), nonce);
    }

    /// Возвращает следующий nonce, увеличивая локальный счётчик.
    pub fn next(&mut self, chain: ChainId, sender: Address) -> u64 {
        let entry = self.state.entry((chain, sender)).or_insert(0);
        let nonce = *entry;
        *entry += 1;
        nonce
    }

    /// Подглядеть следующий nonce без инкремента.
    pub fn peek(&self, chain: ChainId, sender: Address) -> u64 {
        self.state.get(&(chain, sender)).copied().unwrap_or(0)
    }
}

/// Параметры сборки bundle.
#[derive(Debug, Clone, Copy)]
pub struct BuildParams<'a> {
    /// Сеть.
    pub chain_id: ChainId,
    /// Отправитель.
    pub sender: Address,
    /// Целевой контракт.
    pub target: Address,
    /// calldata исполнителя.
    pub calldata: &'a [u8],
    /// Оценка газа.
    pub gas_limit: u64,
    /// Базовая комиссия блока, wei.
    pub base_fee_wei: u128,
    /// Чаевые биддеру, wei.
    pub priority_fee_wei: u128,
    /// Запас к базовой комиссии, %.
    pub fee_bump_pct: u64,
    /// Целевой блок (None — следующий).
    pub target_block: Option<u64>,
}

/// Сборщик bundle: превращает кандидата в готовый к отправке набор транзакций.
pub struct BundleBuilder {
    /// Менеджер nonce.
    pub nonces: NonceManager,
}

impl Default for BundleBuilder {
    fn default() -> Self {
        Self::new()
    }
}

impl BundleBuilder {
    /// Новый сборщик.
    pub fn new() -> Self {
        Self { nonces: NonceManager::new() }
    }

    /// Собирает bundle из параметров кандидата.
    pub fn build(&mut self, params: BuildParams<'_>) -> Bundle {
        let max_fee = fh_chain_access_suggest(params.base_fee_wei, params.priority_fee_wei, params.fee_bump_pct);
        let nonce = self.nonces.next(params.chain_id, params.sender);
        let tx = BundleTx {
            to: params.target,
            data: params.calldata.to_vec(),
            gas: params.gas_limit,
            value: 0,
            max_fee_per_gas: max_fee,
            max_priority_fee_per_gas: params.priority_fee_wei,
            nonce,
            chain_id: params.chain_id.0,
        };
        Bundle {
            chain_id: params.chain_id,
            target: params.target,
            sender: params.sender,
            nonce,
            txs: vec![tx],
            target_block: params.target_block,
        }
    }

    /// Пересобирает bundle для замены: тот же nonce, комиссия выше на bump%.
    ///
    /// Используется, когда блок прошёл без нашего bundle (конкуренты заплатили больше).
    pub fn replace(&self, bundle: &Bundle, bump_pct: u64) -> Bundle {
        let factor = 100 + bump_pct as u128;
        let txs = bundle
            .txs
            .iter()
            .cloned()
            .map(|mut tx| {
                tx.max_fee_per_gas = tx.max_fee_per_gas * factor / 100;
                tx.max_priority_fee_per_gas = tx.max_priority_fee_per_gas * factor / 100;
                tx
            })
            .collect();
        Bundle { txs, ..bundle.clone() }
    }

    /// Отмена: self-transfer с тем же nonce (освобождает nonce и не тратит деньги на газ сверх минимума).
    pub fn cancel(&self, bundle: &Bundle) -> Bundle {
        let tx = BundleTx {
            to: bundle.sender,
            data: Vec::new(),
            gas: 21_000,
            value: 0,
            max_fee_per_gas: bundle.txs.first().map(|tx| tx.max_fee_per_gas * 115 / 100).unwrap_or(0),
            max_priority_fee_per_gas: bundle.txs.first().map(|tx| tx.max_priority_fee_per_gas).unwrap_or(0),
            nonce: bundle.nonce,
            chain_id: bundle.chain_id.0,
        };
        Bundle { txs: vec![tx], ..bundle.clone() }
    }
}

/// Расчёт maxFeePerGas с запасом (аналог fh_chain_access::suggest_max_fee).
fn fh_chain_access_suggest(base_fee_wei: u128, priority_fee_wei: u128, bump_pct: u64) -> u128 {
    base_fee_wei * (100 + bump_pct as u128) / 100 + priority_fee_wei
}

/// Транзакция-чаевые билдеру (часть релеев требует явную «плату за включение»).
pub fn tip_tx(_sender: Address, builder: Address, tip_usd: f64, native_price_usd: f64, nonce: u64, chain: ChainId) -> BundleTx {
    let value = if native_price_usd > 0.0 { (tip_usd / native_price_usd * 1e18) as u128 } else { 0 };
    BundleTx {
        to: builder,
        data: Vec::new(),
        gas: 21_000,
        value,
        max_fee_per_gas: gwei_to_wei(30.0),
        max_priority_fee_per_gas: gwei_to_wei(1.0),
        nonce,
        chain_id: chain.0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params<'a>(calldata: &'a [u8]) -> BuildParams<'a> {
        BuildParams {
            chain_id: ChainId::MAINNET,
            sender: Address([0xaa; 20]),
            target: Address([0xbb; 20]),
            calldata,
            gas_limit: 340_000,
            base_fee_wei: gwei_to_wei(20.0),
            priority_fee_wei: gwei_to_wei(2.0),
            fee_bump_pct: 25,
            target_block: Some(21_000_001),
        }
    }

    #[test]
    fn build_creates_single_tx_with_nonce_and_fees() {
        let mut builder = BundleBuilder::new();
        builder.nonces.sync(ChainId::MAINNET, Address([0xaa; 20]), 7);
        let bundle = builder.build(params(&[0xde, 0xad, 0xbe, 0xef]));
        assert_eq!(bundle.nonce, 7);
        assert_eq!(bundle.txs.len(), 1);
        assert_eq!(bundle.txs[0].max_fee_per_gas, gwei_to_wei(27.0));
        assert_eq!(bundle.txs[0].max_priority_fee_per_gas, gwei_to_wei(2.0));
        assert_eq!(bundle.txs[0].gas, 340_000);
    }

    #[test]
    fn bundle_hash_is_deterministic_and_changes_with_content() {
        let mut builder = BundleBuilder::new();
        let first = builder.build(params(&[1, 2, 3]));
        let second = builder.build(params(&[1, 2, 3]));
        let third = builder.build(params(&[9, 9, 9]));
        assert_eq!(first.hash(), first.hash());
        assert_ne!(first.hash(), third.hash());
        assert!(second.hash().starts_with("0x"));
    }

    #[test]
    fn replacement_keeps_nonce_and_raises_fee() {
        let mut builder = BundleBuilder::new();
        let bundle = builder.build(params(&[7]));
        let replacement = builder.replace(&bundle, 15);
        assert_eq!(replacement.nonce, bundle.nonce);
        assert!(replacement.txs[0].max_fee_per_gas > bundle.txs[0].max_fee_per_gas);
        assert_eq!(replacement.txs[0].max_fee_per_gas, bundle.txs[0].max_fee_per_gas * 115 / 100);
    }

    #[test]
    fn cancel_uses_self_transfer_and_same_nonce() {
        let mut builder = BundleBuilder::new();
        let bundle = builder.build(params(&[7]));
        let cancel = builder.cancel(&bundle);
        assert_eq!(cancel.nonce, bundle.nonce);
        assert_eq!(cancel.txs[0].to, bundle.sender);
        assert_eq!(cancel.txs[0].gas, 21_000);
        assert!(cancel.txs[0].data.is_empty());
    }

    #[test]
    fn rpc_payload_matches_eth_send_bundle_shape() {
        let mut builder = BundleBuilder::new();
        let bundle = builder.build(params(&[0xaa]));
        let payload = bundle.to_rpc_params();
        let array = payload.as_array().expect("payload — массив");
        assert!(array[0]["txs"].is_array());
        assert_eq!(array[0]["txs"][0]["chainId"], "0x1");
        assert!(array[0]["txs"][0]["data"].as_str().expect("data hex").starts_with("0x"));
    }

    #[test]
    fn nonce_manager_is_monotonic_per_wallet() {
        let mut manager = NonceManager::new();
        manager.sync(ChainId::MAINNET, Address([1; 20]), 100);
        assert_eq!(manager.next(ChainId::MAINNET, Address([1; 20])), 100);
        assert_eq!(manager.next(ChainId::MAINNET, Address([1; 20])), 101);
        assert_eq!(manager.peek(ChainId::MAINNET, Address([1; 20])), 102);
        // другой кошелёк — свой счётчик
        assert_eq!(manager.next(ChainId::MAINNET, Address([2; 20])), 0);
    }

    #[test]
    fn tip_tx_keeps_gas_low_and_value_positive() {
        let tx = tip_tx(Address([1; 20]), Address([2; 20]), 50.0, 2_500.0, 5, ChainId::BASE);
        assert_eq!(tx.gas, 21_000);
        assert!(tx.value > 0);
        assert_eq!(tx.chain_id, ChainId::BASE.0);
    }
}
