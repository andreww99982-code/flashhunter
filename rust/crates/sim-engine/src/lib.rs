//! Движок симуляции: проверка кандидата до отправки в сеть.
//!
//! Бюджет 8 мс (ТЗ §1.2). Стратегия уже посчитала «бумажную» прибыль; задача
//! симулятора — проверить, выживет ли сделка при текущем состоянии пулов,
//! с реальным размером газа и с учётом конкуренции за блок.
//!
//! Здесь реализована локальная модель (без EVM): она в разы быстрее и достаточна
//! для фильтра. Точный revm-прогон выполняется вторым этапом.

use fh_pool_indexer::{PoolKind, PoolState};
use fh_shared::ChainId;
use serde::{Deserialize, Serialize};

/// Результат симуляции кандидата.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SimulationOutcome {
    /// Прошла ли симуляция (сделка будет отправлена).
    pub success: bool,
    /// Выход последнего хопа (raw units).
    pub output: u128,
    /// Оценка газа (units).
    pub gas_used: u64,
    /// Чистая прибыль после газа и комиссии флеш-кредита, USD.
    pub net_profit_usd: f64,
    /// Причина отказа (для журнала и разбора инцидентов).
    pub revert_reason: String,
}

/// Причина отказа в терминах, понятных оператору (совпадает с Solidity-ошибками).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RevertReason {
    /// Выход меньше минимально приемлемого (сделка убыточна при исполнении).
    InsufficientOutputAmount,
    /// Прибыль отрицательная (экономика не сходится).
    Unprofitable,
    /// Данные пула устарели (цена уже не та).
    StalePrice,
    /// Пул не найден в индексе (возможно, он новый).
    PoolNotFound,
    /// Комиссия бриджа съела спред (cross-chain интенты).
    BridgeFeeTooHigh,
}

impl RevertReason {
    /// Строка причины в стиле Solidity (для журнала и UI).
    pub fn as_solidity(self) -> &'static str {
        match self {
            RevertReason::InsufficientOutputAmount => "InsufficientOutputAmount()",
            RevertReason::Unprofitable => "Unprofitable()",
            RevertReason::StalePrice => "StalePrice()",
            RevertReason::PoolNotFound => "PoolNotFound()",
            RevertReason::BridgeFeeTooHigh => "BridgeFeeTooHigh()",
        }
    }
}

/// Параметры симуляции (берутся из конфигурации).
#[derive(Debug, Clone, Copy)]
pub struct SimConfig {
    /// Потолок газа в gwei: выше — сделка не отправляется.
    pub max_gas_gwei: f64,
    /// Комиссия флеш-кредита в bps (5 = 0.05%, как у Aave V3).
    pub flash_fee_bps: u64,
    /// Минимальная чистая прибыль, USD.
    pub min_profit_usd: f64,
    /// Цена нативного токена в USD (для перевода газа в деньги).
    pub native_price_usd: f64,
    /// Допустимое устаревание данных пула, мс.
    pub max_staleness_ms: u64,
}

impl Default for SimConfig {
    fn default() -> Self {
        Self {
            max_gas_gwei: 60.0,
            flash_fee_bps: 5,
            min_profit_usd: 10.0,
            native_price_usd: 2_650.0,
            max_staleness_ms: 1_500,
        }
    }
}

/// Смета газа для разных видов сделок (units).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TradeKind {
    /// Арбитражный цикл через флеш-кредит.
    Cycle,
    /// Ликвидация позиции.
    Liquidation,
    /// JIT-ликвидность (mint + burn).
    Jit,
    /// Cross-chain интент.
    Intent,
}

impl TradeKind {
    /// Оценка газа по виду сделки.
    pub fn gas_units(self) -> u64 {
        match self {
            TradeKind::Cycle => 340_000,
            TradeKind::Liquidation => 420_000,
            TradeKind::Jit => 250_000,
            TradeKind::Intent => 380_000,
        }
    }

    /// Дополнительные константы газа (Aave flashLoanSimple и прочие).
    pub fn overhead_units(self) -> u64 {
        match self {
            TradeKind::Cycle | TradeKind::Intent => 60_000,
            TradeKind::Liquidation => 90_000,
            TradeKind::Jit => 0,
        }
    }
}

/// Локальный симулятор: моделирует свопы по состоянию пулов из индекса.
pub struct LocalSimulator {
    /// Конфигурация симуляции.
    pub config: SimConfig,
}

impl LocalSimulator {
    /// Создаёт симулятор с параметрами.
    pub fn new(config: SimConfig) -> Self {
        Self { config }
    }

    /// Симулирует маршрут: последовательность пулов с направлением свопа.
    ///
    /// `route` — список (пул, zero_for_one). Возвращает итог с газом и прибылью.
    pub fn simulate_route(
        &self,
        chain_id: ChainId,
        kind: TradeKind,
        route: &[(&PoolState, bool)],
        amount_in: u128,
        base_fee_gwei: f64,
    ) -> SimulationOutcome {
        if route.is_empty() {
            return self.reject(RevertReason::PoolNotFound, kind);
        }
        if base_fee_gwei > self.config.max_gas_gwei {
            return self.reject_reason(RevertReason::Unprofitable, "gas_above_ceiling", kind);
        }
        for (pool, _) in route {
            if pool.chain_id != chain_id {
                return self.reject(RevertReason::PoolNotFound, kind);
            }
            if pool.is_stale(std::time::Duration::from_millis(self.config.max_staleness_ms)) {
                return self.reject(RevertReason::StalePrice, kind);
            }
        }

        let mut amount = amount_in;
        for (pool, zero_for_one) in route {
            amount = match pool.kind {
                PoolKind::ConstantProduct => pool.constant_product_out(amount, *zero_for_one),
                // V3 и StableSwap: оценка по резервам с поправкой на концентрацию
                // (точный расчёт делает Python-математика / revm на втором этапе).
                PoolKind::Concentrated | PoolKind::StableSwap => {
                    let concentrated = pool.constant_product_out(amount, *zero_for_one);
                    concentrated * 1005 / 1000
                }
            };
            if amount == 0 {
                return self.reject(RevertReason::InsufficientOutputAmount, kind);
            }
        }

        let gross = amount as i128 - amount_in as i128;
        let gas_units = kind.gas_units() + kind.overhead_units();
        let gas_cost_usd = gas_units as f64 * base_fee_gwei * 1e-9 * self.config.native_price_usd;
        // Комиссия флеш-кредита берётся от размера займа
        let flash_fee_usd = amount_in as f64 * self.config.flash_fee_bps as f64 / 10_000.0 * 1.0;
        let net_profit_usd = gross as f64 - gas_cost_usd - flash_fee_usd;

        if gross <= 0 {
            return SimulationOutcome {
                success: false,
                output: amount,
                gas_used: gas_units,
                net_profit_usd,
                revert_reason: RevertReason::Unprofitable.as_solidity().to_string(),
            };
        }
        if net_profit_usd < self.config.min_profit_usd {
            return SimulationOutcome {
                success: false,
                output: amount,
                gas_used: gas_units,
                net_profit_usd,
                revert_reason: RevertReason::Unprofitable.as_solidity().to_string(),
            };
        }

        SimulationOutcome { success: true, output: amount, gas_used: gas_units, net_profit_usd, revert_reason: String::new() }
    }

    fn reject(&self, reason: RevertReason, kind: TradeKind) -> SimulationOutcome {
        SimulationOutcome {
            success: false,
            output: 0,
            gas_used: kind.gas_units(),
            net_profit_usd: 0.0,
            revert_reason: reason.as_solidity().to_string(),
        }
    }

    fn reject_reason(&self, reason: RevertReason, _note: &str, kind: TradeKind) -> SimulationOutcome {
        self.reject(reason, kind)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    fn pool(kind: PoolKind, reserve0: u128, reserve1: u128) -> PoolState {
        PoolState {
            address: fh_shared::Address([3u8; 20]),
            chain_id: ChainId::MAINNET,
            kind,
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
    fn profitable_cycle_passes() {
        let simulator = LocalSimulator::new(SimConfig { min_profit_usd: 1.0, ..Default::default() });
        let buy = pool(PoolKind::ConstantProduct, 1_000_000_000, 1_020_000_000);
        let sell = pool(PoolKind::ConstantProduct, 1_020_000_000, 1_000_000_000);
        let outcome = simulator.simulate_route(
            ChainId::MAINNET,
            TradeKind::Cycle,
            &[(&buy, true), (&sell, false)],
            1_000_000,
            10.0,
        );
        assert!(outcome.success, "прибыльный цикл должен пройти: {:?}", outcome);
        assert!(outcome.net_profit_usd > 1.0);
        assert_eq!(outcome.gas_used, 400_000);
    }

    #[test]
    fn flat_market_is_rejected_as_unprofitable() {
        let simulator = LocalSimulator::new(SimConfig { min_profit_usd: 10.0, ..Default::default() });
        let buy = pool(PoolKind::ConstantProduct, 1_000_000_000, 1_000_000_000);
        let sell = pool(PoolKind::ConstantProduct, 1_000_000_000, 1_000_000_000);
        let outcome = simulator.simulate_route(
            ChainId::MAINNET,
            TradeKind::Cycle,
            &[(&buy, true), (&sell, false)],
            1_000_000,
            10.0,
        );
        assert!(!outcome.success);
        assert_eq!(outcome.revert_reason, "Unprofitable()");
    }

    #[test]
    fn gas_ceiling_blocks_execution() {
        let simulator = LocalSimulator::new(SimConfig { max_gas_gwei: 20.0, ..Default::default() });
        let buy = pool(PoolKind::ConstantProduct, 1_000_000_000, 1_000_000_000);
        let outcome = simulator.simulate_route(ChainId::MAINNET, TradeKind::Cycle, &[(&buy, true)], 1_000, 120.0);
        assert!(!outcome.success);
        assert_eq!(outcome.revert_reason, "Unprofitable()");
    }

    #[test]
    fn stale_pool_is_rejected() {
        let simulator = LocalSimulator::new(SimConfig { max_staleness_ms: 1, ..Default::default() });
        let mut state = pool(PoolKind::ConstantProduct, 1_000, 1_000);
        state.updated_at = Instant::now() - std::time::Duration::from_secs(1);
        let outcome = simulator.simulate_route(ChainId::MAINNET, TradeKind::Cycle, &[(&state, true)], 100, 5.0);
        assert!(!outcome.success);
        assert_eq!(outcome.revert_reason, "StalePrice()");
    }

    #[test]
    fn empty_route_reports_pool_not_found() {
        let simulator = LocalSimulator::new(SimConfig::default());
        let outcome = simulator.simulate_route(ChainId::MAINNET, TradeKind::Cycle, &[], 100, 5.0);
        assert_eq!(outcome.revert_reason, "PoolNotFound()");
    }

    #[test]
    fn trade_kinds_have_distinct_gas_profiles() {
        assert!(TradeKind::Liquidation.gas_units() > TradeKind::Jit.gas_units());
        assert_eq!(TradeKind::Jit.overhead_units(), 0);
    }
}
