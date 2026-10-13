//! Общие типы и конфигурация FlashHunter.
//!
//! Сюда вынесено всё, что используют и Rust-сервисы, и Python-часть:
//! адреса, идентификаторы сетей, единицы измерения и структура конфигурации.
//! Держим крейт без зависимостей от web3 — он компилируется мгновенно и
//! подключается в тестах любого другого крейта.

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

/// Идентификатор сети EVM (chain id). Обёртка запрещает путать его с номером блока.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ChainId(pub u64);

impl ChainId {
    /// Ethereum mainnet.
    pub const MAINNET: ChainId = ChainId(1);
    /// Base.
    pub const BASE: ChainId = ChainId(8453);
    /// Arbitrum One.
    pub const ARBITRUM: ChainId = ChainId(42_161);
    /// Оптимизм.
    pub const OPTIMISM: ChainId = ChainId(10);
    /// Polygon PoS.
    pub const POLYGON: ChainId = ChainId(137);

    /// Основная сеть капитала (там сосредоточена ликвидность WETH/USDC).
    pub fn is_mainnet(self) -> bool {
        self == ChainId::MAINNET
    }
}

impl fmt::Display for ChainId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Адрес в EVM (20 байт). Хранится как массив — без аллокаций в горячем пути.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Address(pub [u8; 20]);

impl Address {
    /// Нулевой адрес (используется как «не задано»).
    pub const ZERO: Address = Address([0u8; 20]);

    /// Разбирает hex-строку с префиксом 0x.
    pub fn from_hex(value: &str) -> Result<Self, SharedError> {
        let clean = value.trim_start_matches("0x").trim_start_matches("0X");
        if clean.len() != 40 {
            return Err(SharedError::BadAddress(value.to_string()));
        }
        let mut bytes = [0u8; 20];
        for (index, chunk) in clean.as_bytes().chunks(2).enumerate() {
            let part = std::str::from_utf8(chunk).map_err(|_| SharedError::BadAddress(value.to_string()))?;
            bytes[index] = u8::from_str_radix(part, 16).map_err(|_| SharedError::BadAddress(value.to_string()))?;
        }
        Ok(Address(bytes))
    }

    /// Возвращает checksum-независимое hex-представление (lowercase).
    pub fn to_lower_hex(self) -> String {
        let mut out = String::with_capacity(42);
        out.push_str("0x");
        for byte in self.0 {
            out.push_str(&format!("{byte:02x}"));
        }
        out
    }

    /// Короткая форма для логов: 0x1234…cdef.
    pub fn short(self) -> String {
        let hex = self.to_lower_hex();
        format!("{}…{}", &hex[..6], &hex[hex.len() - 4..])
    }
}

impl fmt::Display for Address {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.to_lower_hex())
    }
}

impl FromStr for Address {
    type Err = SharedError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Address::from_hex(s)
    }
}

/// Ошибки разделяемого слоя.
#[derive(Debug, thiserror::Error)]
pub enum SharedError {
    /// Адрес не соответствует формату 0x + 40 hex-символов.
    #[error("некорректный адрес: {0}")]
    BadAddress(String),
    /// Настройка отсутствует в окружении.
    #[error("переменная окружения {0} не задана")]
    MissingEnv(&'static str),
    /// Значение настройки не парсится.
    #[error("некорректное значение {0}: {1}")]
    BadValue(&'static str, String),
}

/// Конфигурация Rust-сервисов: читается из окружения (совпадает с .env Python-части).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// RPC-эндпоинты по сетям (http).
    pub rpc_urls: Vec<(ChainId, String)>,
    /// WebSocket-эндпоинты (нужны для mempool и новых блоков).
    pub ws_urls: Vec<(ChainId, String)>,
    /// Требуется ли dry-run (в dry-run bundle не уходит в релей).
    pub dry_run: bool,
    /// Порог газа, выше которого сделки не отправляются.
    pub max_gas_gwei: f64,
    /// Минимальная прибыль в USD для отправки bundle.
    pub min_profit_usd: f64,
}

impl Config {
    /// Читает конфигурацию из переменных окружения.
    ///
    /// Формат RPC: `FLASHHUNTER_RPC_URLS=1=https://eth...,8453=https://base...`
    pub fn from_env() -> Result<Self, SharedError> {
        let raw = std::env::var("FLASHHUNTER_RPC_URLS").map_err(|_| SharedError::MissingEnv("FLASHHUNTER_RPC_URLS"))?;
        let mut rpc_urls = Vec::new();
        let mut ws_urls = Vec::new();
        for entry in raw.split(',').filter(|item| !item.trim().is_empty()) {
            let (chain, url) = entry
                .split_once('=')
                .ok_or_else(|| SharedError::BadValue("FLASHHUNTER_RPC_URLS", entry.to_string()))?;
            let chain: u64 = chain
                .trim()
                .parse()
                .map_err(|_| SharedError::BadValue("FLASHHUNTER_RPC_URLS", entry.to_string()))?;
            rpc_urls.push((ChainId(chain), url.trim().to_string()));
        }
        if let Ok(ws_raw) = std::env::var("FLASHHUNTER_WS_URLS") {
            for entry in ws_raw.split(',').filter(|item| !item.trim().is_empty()) {
                if let Some((chain, url)) = entry.split_once('=') {
                    if let Ok(chain) = chain.trim().parse() {
                        ws_urls.push((ChainId(chain), url.trim().to_string()));
                    }
                }
            }
        }
        let dry_run = std::env::var("FLASHHUNTER_DRY_RUN").map(|v| v != "false").unwrap_or(true);
        let max_gas_gwei = std::env::var("EXEC_MAX_GAS_GWEI")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(60.0);
        let min_profit_usd = std::env::var("EXEC_MIN_PROFIT_USD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(10.0);
        Ok(Config { rpc_urls, ws_urls, dry_run, max_gas_gwei, min_profit_usd })
    }

    /// Список сетей, для которых настроен RPC.
    pub fn chains(&self) -> Vec<ChainId> {
        self.rpc_urls.iter().map(|(chain, _)| *chain).collect()
    }
}

/// Единица измерения газа: wei за газ (используется в bundle и симуляциях).
pub fn gwei_to_wei(gwei: f64) -> u128 {
    (gwei * 1e9) as u128
}

/// Обратное преобразование для логов и метрик.
pub fn wei_to_gwei(wei: u128) -> f64 {
    wei as f64 / 1e9
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn address_parsing_roundtrip() {
        let raw = "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc";
        let address = Address::from_hex(raw).expect("адрес должен парситься");
        assert_eq!(address.to_lower_hex(), raw.to_lowercase());
        assert!(address.short().contains('…'));
    }

    #[test]
    fn address_rejects_bad_input() {
        assert!(Address::from_hex("0x1234").is_err());
        assert!(Address::from_hex("not-an-address").is_err());
    }

    #[test]
    fn chain_id_helpers() {
        assert!(ChainId::MAINNET.is_mainnet());
        assert!(!ChainId::BASE.is_mainnet());
        assert_eq!(ChainId::ARBITRUM.to_string(), "42161");
    }

    #[test]
    fn gas_conversions_are_symmetric() {
        assert_eq!(gwei_to_wei(30.0), 30_000_000_000);
        assert!((wei_to_gwei(30_000_000_000) - 30.0).abs() < f64::EPSILON);
    }
}
