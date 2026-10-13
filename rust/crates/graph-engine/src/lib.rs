//! Граф токенов и поиск циклов (арбитражные циклы 2–4 хопа).
//!
//! Идея: строим граф «токен → пулы», где каждое ребро — пул с известной глубиной
//! и комиссией. Затем ищем циклы, начинающиеся и заканчивающиеся в якорном токене
//! (WETH/USDC/USDT/WBTC), и оцениваем верхнюю границу прибыли по текущим курсам.
//!
//! Граф перестраивается на каждом блоке: пул может появиться (новая пара) или
//! стать неликвидным. Поиск ограничен по глубине и по числу узлов, чтобы
//! укладываться в бюджет 3 мс (ТЗ §1.2).

use fh_shared::Address;
use std::collections::{HashMap, HashSet};

/// Ребро графа: пул, по которому можно обменять token_in → token_out.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Edge {
    /// Пулы-адрес.
    pub pool: Address,
    /// Входной токен.
    pub token_in: Address,
    /// Выходной токен.
    pub token_out: Address,
    /// Комиссия пула в bps.
    pub fee_bps: u32,
    /// Оценка глубины в USD (нужна для приоритизации).
    pub depth_usd: u64,
}

/// Ошибки построения графа.
#[derive(Debug, thiserror::Error)]
pub enum GraphError {
    /// Превышена максимальная глубина цикла (защита от комбинаторного взрыва).
    #[error("глубина цикла {0} превышает максимум {1}")]
    DepthExceeded(usize, usize),
    /// Якорный токен отсутствует в графе.
    #[error("якорный токен {0} не найден в графе")]
    UnknownAnchor(Address),
}

/// Граф токенов с индексированием по исходящим рёбрам.
#[derive(Debug, Default, Clone)]
pub struct TokenGraph {
    outgoing: HashMap<Address, Vec<Edge>>,
    edge_count: usize,
}

impl TokenGraph {
    /// Пустой граф.
    pub fn new() -> Self {
        Self::default()
    }

    /// Добавляет ребро (пул) в граф. Дубликаты по (пул, направление) не создаются.
    pub fn add_edge(&mut self, edge: Edge) {
        let bucket = self.outgoing.entry(edge.token_in).or_default();
        if bucket
            .iter()
            .any(|item| item.pool == edge.pool && item.token_out == edge.token_out)
        {
            return;
        }
        bucket.push(edge);
        self.edge_count += 1;
    }

    /// Число рёбер в графе.
    pub fn edge_count(&self) -> usize {
        self.edge_count
    }

    /// Число токенов-узлов.
    pub fn node_count(&self) -> usize {
        self.outgoing.len()
    }

    /// Исходящие рёбра токена.
    pub fn edges_from(&self, token: &Address) -> &[Edge] {
        self.outgoing.get(token).map(|list| list.as_slice()).unwrap_or(&[])
    }

    /// Ищет циклы от якорного токена с ограничением глубины.
    ///
    /// Возвращает список циклов; каждый цикл — последовательность рёбер. Цикл
    /// считается найденным, если он возвращается в якорь, а все промежуточные
    /// токены уникальны (повтор узла внутри цикла даёт мусорные маршруты).
    pub fn find_cycles(&self, anchor: &Address, max_depth: usize) -> Result<Vec<Vec<Edge>>, GraphError> {
        if max_depth > 4 {
            // 4 хопа — практический предел: газ растёт, а прибыль почти не растёт
            return Err(GraphError::DepthExceeded(max_depth, 4));
        }
        if self.edges_from(anchor).is_empty() {
            return Err(GraphError::UnknownAnchor(*anchor));
        }
        let mut cycles = Vec::new();
        let mut path: Vec<Edge> = Vec::with_capacity(max_depth);
        let mut visited: HashSet<Address> = HashSet::new();
        visited.insert(*anchor);
        self.dfs(anchor, anchor, max_depth, &mut path, &mut visited, &mut cycles);
        Ok(cycles)
    }

    fn dfs(
        &self,
        anchor: &Address,
        current: &Address,
        depth_left: usize,
        path: &mut Vec<Edge>,
        visited: &mut HashSet<Address>,
        cycles: &mut Vec<Vec<Edge>>,
    ) {
        if depth_left == 0 || cycles.len() >= 2_000 {
            return;
        }
        for edge in self.edges_from(current) {
            if edge.token_out == *anchor {
                let mut cycle = path.clone();
                cycle.push(*edge);
                if cycle.len() >= 2 {
                    cycles.push(cycle);
                }
                continue;
            }
            if visited.contains(&edge.token_out) {
                continue;
            }
            visited.insert(edge.token_out);
            path.push(*edge);
            self.dfs(anchor, &edge.token_out, depth_left - 1, path, visited, cycles);
            path.pop();
            visited.remove(&edge.token_out);
        }
    }

    /// Верхняя граница прибыли цикла по комиссиям: произведение (1 − fee) для всех хопов.
    ///
    /// Если произведение меньше 1, цикл заведомо убыточен по комиссиям и его можно
    /// отбросить до дорогой оптимизации размера (экономит бюджет латентности).
    pub fn fee_product_viable(cycle: &[Edge]) -> bool {
        let mut product = 1.0_f64;
        for edge in cycle {
            product *= 1.0 - edge.fee_bps as f64 / 10_000.0;
        }
        product > 0.97 // запас: остаётся место для спреда и газа
    }

    /// Суммарная глубина цикла в USD (минимальная глубина по хопам = узкое место).
    pub fn cycle_depth_usd(cycle: &[Edge]) -> u64 {
        cycle.iter().map(|edge| edge.depth_usd).min().unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn address(seed: u8) -> Address {
        Address([seed; 20])
    }

    fn edge(pool: u8, from: u8, to: u8, fee_bps: u32, depth_usd: u64) -> Edge {
        Edge { pool: address(pool), token_in: address(from), token_out: address(to), fee_bps, depth_usd }
    }

    fn triangle_graph() -> TokenGraph {
        // WETH(1) → USDC(2) → USDT(3) → WETH(1)
        let mut graph = TokenGraph::new();
        graph.add_edge(edge(10, 1, 2, 5, 5_000_000));
        graph.add_edge(edge(11, 2, 3, 1, 4_000_000));
        graph.add_edge(edge(12, 3, 1, 30, 3_000_000));
        graph
    }

    #[test]
    fn finds_triangular_cycle() {
        let graph = triangle_graph();
        let cycles = graph.find_cycles(&address(1), 4).expect("цикл должен найтись");
        assert_eq!(cycles.len(), 1);
        assert_eq!(cycles[0].len(), 3);
        assert_eq!(cycles[0][0].token_in, address(1));
        assert_eq!(cycles[0][2].token_out, address(1));
    }

    #[test]
    fn ignores_edges_without_return_path() {
        let mut graph = TokenGraph::new();
        graph.add_edge(edge(10, 1, 2, 5, 1_000_000));
        let cycles = graph.find_cycles(&address(1), 4).expect("граф валиден");
        assert!(cycles.is_empty());
    }

    #[test]
    fn depth_limit_is_enforced() {
        let graph = triangle_graph();
        assert!(matches!(graph.find_cycles(&address(1), 5), Err(GraphError::DepthExceeded(_, 4))));
    }

    #[test]
    fn unknown_anchor_is_error() {
        let graph = triangle_graph();
        assert!(matches!(graph.find_cycles(&address(99), 3), Err(GraphError::UnknownAnchor(_))));
    }

    #[test]
    fn duplicate_edges_are_not_doubled() {
        let mut graph = TokenGraph::new();
        graph.add_edge(edge(10, 1, 2, 5, 1_000));
        graph.add_edge(edge(10, 1, 2, 5, 1_000));
        assert_eq!(graph.edge_count(), 1);
        assert_eq!(graph.node_count(), 1);
    }

    #[test]
    fn fee_product_filters_unprofitable_cycles() {
        let cheap = vec![edge(1, 1, 2, 5, 1), edge(2, 2, 1, 5, 1)];
        let expensive = vec![edge(3, 1, 2, 300, 1), edge(4, 2, 1, 300, 1)];
        assert!(TokenGraph::fee_product_viable(&cheap));
        assert!(!TokenGraph::fee_product_viable(&expensive));
    }

    #[test]
    fn cycle_depth_is_the_narrowest_hop() {
        let cycle = vec![edge(1, 1, 2, 5, 9_000_000), edge(2, 2, 1, 5, 120_000)];
        assert_eq!(TokenGraph::cycle_depth_usd(&cycle), 120_000);
    }
}
