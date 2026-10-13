/** Стратегии: тумблеры, сети, редактор параметров (JSON) и живая статистика. */
import { useState } from 'react';
import { Save, SlidersHorizontal, Power } from 'lucide-react';
import { api } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { StatusPill } from '../components/StatusPill';
import { useApi } from '../hooks/useApi';
import { useToast } from '../components/Toast';

interface StrategyRow {
  id: number;
  name: string;
  kind: string;
  enabled: boolean;
  chains: number[];
  params: Record<string, unknown>;
  running: boolean;
  latency_ms: number | null;
}

const DESCRIPTIONS: Record<string, string> = {
  dex_arb: 'Классический кросс-DEX арбитраж по паре: X→Y на пуле A, Y→X на пуле B. Оптимум размера — аналитически (V2) или тернарным поиском (V3/Curve).',
  triangular: 'Многоходовые циклы A→B→C→A до 4 хопов. Граф токенов, ограниченный DFS от якорей, оптимизация размера по составной функции прибыли.',
  stable_peg: 'Отклонения стейблкоинов от паритета (1–50 bps). Жёсткий фильтр по базовой комиссии: при дорогом газе окно не существует.',
  liquidation: 'Aave V3 / Compound V3 / Morpho / Spark / Silo / Euler V2 / Fluid: оптимизация debtToCover, бонус ликвидатора минус своп и газ.',
  jit: 'Just-In-Time ликвидность в V3/V4 под крупные свопы: доля комиссии за один блок.',
  backrun: 'Компенсирующий арбитраж после крупных свопов. Sandwich-атаки запрещены политикой проекта (sandwich=false).',
  intent: 'Cross-chain арбитраж через Across / deBridge DLN / LayerZero OFT с учётом комиссии бриджа и ETA.',
};

export function Strategies() {
  const { data, refetch, loading } = useApi<{ items: StrategyRow[] }>('/strategies', 8000);
  const { push } = useToast();
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const toggle = async (name: string, enabled: boolean) => {
    try {
      await api.patch(`/strategies/${name}`, { enabled });
      push('success', `Стратегия ${name} ${enabled ? 'включена' : 'выключена'}`);
      await refetch();
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Не удалось изменить стратегию');
    }
  };

  const saveParams = async (name: string, raw: string) => {
    try {
      const params = JSON.parse(raw) as Record<string, unknown>;
      await api.patch(`/strategies/${name}`, { params });
      push('success', `Параметры ${name} сохранены`);
      await refetch();
    } catch (error) {
      push('error', error instanceof SyntaxError ? 'Некорректный JSON параметров' : 'Ошибка сохранения');
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {(data?.items ?? []).map((strategy) => (
        <GlassCard
          key={strategy.id}
          title={
            <span className="flex items-center gap-2">
              {strategy.name}
              {strategy.enabled ? <StatusPill status="running" label="включена" /> : <StatusPill status="disabled" label="выключена" />}
            </span>
          }
          subtitle={`сети: ${strategy.chains.join(', ')} · kind: ${strategy.kind}${strategy.latency_ms ? ` · latency ${strategy.latency_ms} мс` : ''}`}
          actions={
            <button className={`btn ${strategy.enabled ? 'btn-outline' : 'btn-success'} !px-3`} onClick={() => void toggle(strategy.name, !strategy.enabled)}>
              <Power size={14} /> {strategy.enabled ? 'Выключить' : 'Включить'}
            </button>
          }
        >
          <p className="mb-3 text-xs leading-relaxed text-[var(--text-2)]">{DESCRIPTIONS[strategy.name] ?? 'Стратегия FlashHunter'}</p>
          <label className="mb-1 flex items-center gap-1 text-[11px] uppercase tracking-wider text-[var(--text-3)]">
            <SlidersHorizontal size={11} /> Параметры (JSON)
          </label>
          <textarea
            className="field num h-40 font-mono text-xs"
            spellCheck={false}
            value={drafts[strategy.name] ?? JSON.stringify(strategy.params, null, 2)}
            onChange={(event) => setDrafts((prev) => ({ ...prev, [strategy.name]: event.target.value }))}
          />
          <div className="mt-3 flex gap-2">
            <button
              className="btn btn-tonal"
              onClick={() => void saveParams(strategy.name, drafts[strategy.name] ?? JSON.stringify(strategy.params, null, 2))}
            >
              <Save size={14} /> Сохранить
            </button>
            <button
              className="btn btn-outline"
              onClick={() => setDrafts((prev) => ({ ...prev, [strategy.name]: JSON.stringify(strategy.params, null, 2) }))}
            >
              Сбросить
            </button>
          </div>
        </GlassCard>
      ))}
      {loading && !data && <div className="skeleton h-64 w-full" />}
    </div>
  );
}
