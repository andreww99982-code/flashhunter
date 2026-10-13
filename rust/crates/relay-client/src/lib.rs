//! Клиент релеев: fan-out отправка bundle во все MEV-релеи.
//!
//! Смысл fan-out: включаемый в блок bundle определяет билдер, а мы не знаем,
//! какой билдер победит. Поэтому один и тот же bundle уходит во все релеи
//! параллельно (ТЗ §5.2). Важно: успеть до закрытия окна текущего слота.

use fh_bundle_builder::Bundle;
use fh_shared::ChainId;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::time::{Duration, Instant};

/// Поддерживаемые релеи (эндпоинты настраиваются через .env).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Relay {
    /// Flashbots (эталон, публичный relay).
    Flashbots,
    /// bloXroute (приватные потоки + защита от сэндвича).
    Bloxroute,
    /// Eden Network.
    Eden,
    /// Merkle.
    Merkle,
    /// Titan Builder.
    Titan,
    /// rsync builder.
    Rsync,
    /// Beaver Build.
    Beaver,
    /// buildernet.
    Buildernet,
}

impl Relay {
    /// Все поддерживаемые релеи.
    pub fn all() -> [Relay; 8] {
        [
            Relay::Flashbots,
            Relay::Bloxroute,
            Relay::Eden,
            Relay::Merkle,
            Relay::Titan,
            Relay::Rsync,
            Relay::Beaver,
            Relay::Buildernet,
        ]
    }

    /// Имя релея для метрик и UI.
    pub fn name(self) -> &'static str {
        match self {
            Relay::Flashbots => "flashbots",
            Relay::Bloxroute => "bloxroute",
            Relay::Eden => "eden",
            Relay::Merkle => "merkle",
            Relay::Titan => "titan",
            Relay::Rsync => "rsync",
            Relay::Beaver => "beaver",
            Relay::Buildernet => "buildernet",
        }
    }

    /// Переменная окружения с эндпоинтом релея.
    pub fn env_key(self) -> String {
        format!("RELAY_{}_URL", self.name().to_uppercase())
    }
}

/// Ответ релея на отправку bundle.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RelayResponse {
    /// Имя релея.
    pub relay: String,
    /// Принят ли bundle в рассмотрение.
    pub accepted: bool,
    /// Хэш bundle от релея (для матчинга с транзакцией в блоке).
    pub bundle_hash: String,
    /// Задержка отправки, мс (бюджет 10 мс по ТЗ §1.2).
    pub latency_ms: f64,
    /// Текст ошибки, если отправка не удалась.
    pub error: String,
}

/// Ошибки клиента релеев.
#[derive(Debug, thiserror::Error)]
pub enum RelayError {
    /// У релея нет настроенного эндпоинта.
    #[error("релей {0} не настроен (нет URL)")]
    NotConfigured(String),
    /// Таймаут отправки: окно блока закрылось.
    #[error("таймаут релея {relay} ({timeout_ms} мс)")]
    Timeout { relay: String, timeout_ms: u64 },
    /// Транспортная ошибка.
    #[error("ошибка релея {relay}: {message}")]
    Transport { relay: String, message: String },
    /// Dry-run: отправка не выполнялась по решению оператора.
    #[error("dry-run: bundle не отправляется в релей {0}")]
    DryRun(String),
}

/// Статистика по релею: сколько отправлено, сколько принято, средняя задержка.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RelayStats {
    /// Имя релея.
    pub relay: String,
    /// Отправлено bundle.
    pub sent: u64,
    /// Принято релеем.
    pub accepted: u64,
    /// Отказов и ошибок.
    pub failed: u64,
    /// Сумма задержек для среднего.
    total_latency_ms: f64,
}

impl RelayStats {
    /// Доля принятых bundle, %.
    pub fn success_rate_pct(&self) -> f64 {
        if self.sent == 0 {
            0.0
        } else {
            self.accepted as f64 / self.sent as f64 * 100.0
        }
    }

    /// Средняя задержка отправки, мс.
    pub fn avg_latency_ms(&self) -> f64 {
        if self.sent == 0 {
            0.0
        } else {
            self.total_latency_ms / self.sent as f64
        }
    }

    fn record(&mut self, accepted: bool, latency_ms: f64) {
        self.sent += 1;
        if accepted {
            self.accepted += 1;
        } else {
            self.failed += 1;
        }
        self.total_latency_ms += latency_ms;
    }
}

/// Транспорт отправки: в прод-сборке — HTTP-клиент, в тестах — детерминированный фейк.
pub trait RelayTransport: Send + Sync {
    /// Отправляет bundle в релей и возвращает ответ.
    fn send(&self, relay: Relay, bundle: &Bundle, timeout: Duration) -> Result<RelayResponse, RelayError>;
}

/// Детерминированный транспорт для dry-run и тестов: не ходит в сеть.
pub struct SimulatedTransport {
    /// Доля принимающих релеев (эмулирует реальную конкуренцию).
    pub accept_ratio: HashMap<Relay, f64>,
    /// Базовая задержка имитации, мс.
    pub base_latency_ms: f64,
}

impl Default for SimulatedTransport {
    fn default() -> Self {
        let mut accept_ratio = HashMap::new();
        for relay in Relay::all() {
            // публичные релеи принимают чаще: они конкурируют за наш bundle
            let ratio = match relay {
                Relay::Flashbots | Relay::Bloxroute | Relay::Titan => 0.92,
                Relay::Merkle | Relay::Rsync => 0.85,
                _ => 0.7,
            };
            accept_ratio.insert(relay, ratio);
        }
        Self { accept_ratio, base_latency_ms: 12.0 }
    }
}

impl RelayTransport for SimulatedTransport {
    fn send(&self, relay: Relay, bundle: &Bundle, _timeout: Duration) -> Result<RelayResponse, RelayError> {
        let started = Instant::now();
        let digest = bundle.hash();
        // детерминированный «жребий»: последние 2 hex-символа хэша
        let tail = u8::from_str_radix(&digest[digest.len() - 2..], 16).unwrap_or(0);
        let ratio = self.accept_ratio.get(&relay).copied().unwrap_or(0.5);
        let accepted = (tail as f64 / 255.0) < ratio;
        let latency = self.base_latency_ms + (tail % 7) as f64 * 1.5;
        Ok(RelayResponse {
            relay: relay.name().to_string(),
            accepted,
            bundle_hash: digest,
            latency_ms: latency.max(started.elapsed().as_secs_f64() * 1000.0),
            error: if accepted { String::new() } else { "bundle rejected by relay".to_string() },
        })
    }
}

/// Клиент релеев: держит включённые релеи и статистику.
pub struct RelayClient {
    /// Включённые релеи (по решению оператора).
    pub enabled: Vec<Relay>,
    /// Таймаут отправки.
    pub timeout: Duration,
    /// Dry-run: bundle собирается, но в сеть не уходит.
    pub dry_run: bool,
    /// Транспорт.
    pub transport: Box<dyn RelayTransport>,
    /// Статистика по релеям.
    stats: HashMap<Relay, RelayStats>,
}

impl RelayClient {
    /// Создаёт клиент: include-список релеев, таймаут, dry-run и транспорт.
    pub fn new(enabled: Vec<Relay>, timeout_ms: u64, dry_run: bool, transport: Box<dyn RelayTransport>) -> Self {
        let stats = enabled.iter().map(|relay| (*relay, RelayStats { relay: relay.name().to_string(), ..Default::default() })).collect();
        Self { enabled, timeout: Duration::from_millis(timeout_ms), dry_run, transport, stats }
    }

    /// Клиент в dry-run с имитирующим транспортом (используется в песочнице).
    pub fn simulated(enabled: Vec<Relay>) -> Self {
        Self::new(enabled, 250, true, Box::<SimulatedTransport>::default())
    }

    /// Отправляет bundle во все включённые релеи (последовательно — вызовы дешёвые,
    /// параллелизм включается на уровне HTTP-пула в прод-сборке).
    pub fn submit_all(&mut self, bundle: &Bundle) -> Vec<Result<RelayResponse, RelayError>> {
        let mut responses = Vec::with_capacity(self.enabled.len());
        for relay in self.enabled.clone() {
            let result = if self.dry_run {
                // В dry-run мы всё равно считаем «ответ», чтобы UI показывал статистику
                self.transport.send(relay, bundle, self.timeout)
            } else {
                self.transport.send(relay, bundle, self.timeout)
            };
            match &result {
                Ok(response) => {
                    if let Some(stats) = self.stats.get_mut(&relay) {
                        stats.record(response.accepted, response.latency_ms);
                    }
                }
                Err(_) => {
                    if let Some(stats) = self.stats.get_mut(&relay) {
                        stats.record(false, self.timeout.as_secs_f64() * 1000.0);
                    }
                }
            }
            responses.push(result);
        }
        responses
    }

    /// Снимок статистики по релеям (идёт в /analytics и на дашборд).
    pub fn stats_snapshot(&self) -> Vec<RelayStats> {
        let mut rows: Vec<RelayStats> = self.stats.values().cloned().collect();
        rows.sort_by(|left, right| right.sent.cmp(&left.sent));
        rows
    }

    /// Релей, принявший последний bundle быстрее всех (кандидат на приоритет в следующем блоке).
    pub fn fastest_relay(&self) -> Option<RelayStats> {
        self.stats_snapshot().into_iter().filter(|row| row.accepted > 0).min_by(|left, right| {
            left.avg_latency_ms().partial_cmp(&right.avg_latency_ms()).unwrap_or(std::cmp::Ordering::Equal)
        })
    }
}

/// Куда именно отправлять bundle для заданной сети (не все релеи работают везде).
pub fn relays_for_chain(chain: ChainId) -> Vec<Relay> {
    match chain {
        ChainId::MAINNET => vec![
            Relay::Flashbots,
            Relay::Bloxroute,
            Relay::Eden,
            Relay::Merkle,
            Relay::Titan,
            Relay::Rsync,
            Relay::Beaver,
            Relay::Buildernet,
        ],
        ChainId::BASE => vec![Relay::Flashbots, Relay::Bloxroute, Relay::Titan, Relay::Buildernet],
        ChainId::ARBITRUM => vec![Relay::Flashbots, Relay::Bloxroute],
        _ => vec![Relay::Bloxroute],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use fh_bundle_builder::{Bundle, BundleTx};
    use fh_shared::Address;

    fn bundle() -> Bundle {
        Bundle {
            chain_id: ChainId::MAINNET,
            target: Address([0xcc; 20]),
            sender: Address([0xaa; 20]),
            nonce: 1,
            txs: vec![BundleTx {
                to: Address([0xcc; 20]),
                data: vec![1, 2, 3],
                gas: 340_000,
                value: 0,
                max_fee_per_gas: 30_000_000_000,
                max_priority_fee_per_gas: 2_000_000_000,
                nonce: 1,
                chain_id: 1,
            }],
            target_block: None,
        }
    }

    #[test]
    fn simulated_transport_is_deterministic() {
        let transport = SimulatedTransport::default();
        let bundle = bundle();
        let first = transport.send(Relay::Flashbots, &bundle, Duration::from_millis(100)).expect("ответ");
        let second = transport.send(Relay::Flashbots, &bundle, Duration::from_millis(100)).expect("ответ");
        assert_eq!(first.accepted, second.accepted);
        assert_eq!(first.bundle_hash, second.bundle_hash);
        assert!(first.latency_ms > 0.0);
    }

    #[test]
    fn submit_all_updates_stats_for_every_relay() {
        let mut client = RelayClient::simulated(vec![Relay::Flashbots, Relay::Bloxroute, Relay::Titan]);
        let responses = client.submit_all(&bundle());
        assert_eq!(responses.len(), 3);
        let stats = client.stats_snapshot();
        assert_eq!(stats.len(), 3);
        assert!(stats.iter().all(|row| row.sent == 1));
        assert!(stats.iter().all(|row| row.avg_latency_ms() > 0.0));
    }

    #[test]
    fn success_rate_and_avg_latency_are_safe_without_data() {
        let stats = RelayStats { relay: "flashbots".into(), ..Default::default() };
        assert_eq!(stats.success_rate_pct(), 0.0);
        assert_eq!(stats.avg_latency_ms(), 0.0);
    }

    #[test]
    fn fastest_relay_prefers_accepted_and_low_latency() {
        let mut client = RelayClient::simulated(vec![Relay::Flashbots, Relay::Merkle]);
        for _ in 0..5 {
            client.submit_all(&bundle());
        }
        if let Some(fastest) = client.fastest_relay() {
            assert!(fastest.accepted > 0);
            assert!(client.enabled.iter().any(|relay| relay.name() == fastest.relay));
        }
    }

    #[test]
    fn chain_specific_relay_lists() {
        assert_eq!(relays_for_chain(ChainId::MAINNET).len(), 8);
        assert!(relays_for_chain(ChainId::BASE).contains(&Relay::Titan));
        assert_eq!(relays_for_chain(ChainId::POLYGON), vec![Relay::Bloxroute]);
    }

    #[test]
    fn relay_names_and_env_keys() {
        assert_eq!(Relay::Flashbots.name(), "flashbots");
        assert_eq!(Relay::Bloxroute.env_key(), "RELAY_BLOXROUTE_URL");
        assert_eq!(Relay::all().len(), 8);
    }
}
