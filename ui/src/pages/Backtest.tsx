/** Бэктест: прогон стратегий на историческом/синтетическом рынке и сравнение параметров. */
import { useMemo, useState } from 'react';
import { FlaskConical, Play, Timer } from 'lucide-react';
import { api } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { KpiTile } from '../components/KpiTile';
import { Chart, axisStyle, glassTooltip } from '../components/Chart';
import { DataTable, type Column } from '../components/DataTable';
import { useApi } from '../hooks/useApi';
import { useToast } from '../components/Toast';

interface BacktestResult {
  params: Record<string, unknown>;
  blocks: number;
  duration_sec: number;
  blocks_per_sec: number;
  total_found: number;
  total_net_usd: number;
  avg_per_block_usd: number;
  by_strategy: { strategy: string; found: number; net_usd: number; best_usd: number; avg_size_usd: number }[];
  equity_curve: number[];
  note: string;
}

const ALL_STRATEGIES = ['dex_arb', 'triangular', 'stable_peg', 'liquidation', 'jit', 'backrun', 'intent'];

export function Backtest() {
  const profiles = useApi<{ profiles: string[]; strategies: string[] }>('/backtest/profiles', 0);
  const { push } = useToast();
  const [blocks, setBlocks] = useState(300);
  const [seed, setSeed] = useState(11);
  const [profile, setProfile] = useState('volatile');
  const [minProfit, setMinProfit] = useState(10);
  const [selected, setSelected] = useState<string[]>(['dex_arb', 'triangular', 'stable_peg', 'liquidation']);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [history, setHistory] = useState<{ label: string; net: number; found: number; blocks: number }[]>([]);

  const equityOption = useMemo(
    () => ({
      tooltip: { trigger: 'axis' as const, ...glassTooltip },
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      xAxis: { type: 'category' as const, data: (result?.equity_curve ?? []).map((_, index) => String(index + 1)), ...axisStyle },
      yAxis: { type: 'value' as const, ...axisStyle },
      series: [
        {
          name: 'Кумулятивный net PnL, $',
          type: 'line' as const,
          smooth: true,
          showSymbol: false,
          data: (result?.equity_curve ?? []).map((value) => value.toFixed(2)),
          lineStyle: { color: '#7c5cff', width: 2.5 },
          areaStyle: {
            color: {
              type: 'linear' as const,
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(124,92,255,0.4)' },
                { offset: 1, color: 'rgba(124,92,255,0.02)' },
              ],
            },
          },
        },
      ],
    }),
    [result],
  );

  const run = async () => {
    setRunning(true);
    try {
      const response = await api.post<BacktestResult>('/backtest/run', {
        blocks,
        seed,
        profile,
        strategies: selected,
        min_profit_usd: minProfit,
      });
      setResult(response);
      setHistory((prev) =>
        [
          {
            label: `${profile} · ${seed} · ${blocks} бл.`,
            net: response.total_net_usd,
            found: response.total_found,
            blocks: response.blocks,
          },
          ...prev,
        ].slice(0, 8),
      );
      push('success', `Прогон завершён: ${response.total_found} возможностей, net $${response.total_net_usd.toFixed(2)}`);
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Ошибка бэктеста');
    } finally {
      setRunning(false);
    }
  };

  const runColumns: Column<{ label: string; net: number; found: number; blocks: number }>[] = [
    { key: 'label', title: 'Прогон', render: (row) => <span className="num text-xs">{row.label}</span> },
    { key: 'found', title: 'Найдено', render: (row) => <span className="num">{row.found}</span> },
    {
      key: 'net',
      title: 'Net PnL',
      render: (row) => <span className={`num font-semibold ${row.net >= 0 ? 'profit' : 'loss'}`}>${row.net.toFixed(2)}</span>,
    },
    {
      key: 'perblock',
      title: '$ / блок',
      render: (row) => <span className="num text-xs">${(row.net / Math.max(1, row.blocks)).toFixed(4)}</span>,
    },
  ];

  const strategyColumns: Column<BacktestResult['by_strategy'][number]>[] = [
    { key: 'strategy', title: 'Стратегия', render: (row) => <span className="pill pill-primary">{row.strategy}</span> },
    { key: 'found', title: 'Найдено', render: (row) => <span className="num">{row.found}</span> },
    { key: 'net', title: 'Net PnL', render: (row) => <span className={`num font-semibold ${row.net_usd >= 0 ? 'profit' : 'loss'}`}>${row.net_usd.toFixed(2)}</span> },
    { key: 'best', title: 'Лучшая', render: (row) => <span className="num">${row.best_usd.toFixed(2)}</span> },
    { key: 'avg', title: 'Ср. размер', render: (row) => <span className="num">${row.avg_size_usd.toLocaleString('ru-RU', { maximumFractionDigits: 0 })}</span> },
    {
      key: 'per_block',
      title: '$ / блок',
      render: (row) => <span className="num text-xs">${(row.net_usd / Math.max(1, blocks)).toFixed(3)}</span>,
    },
  ];

  return (
    <div className="space-y-4">
      <GlassCard title="Параметры прогона" subtitle="данные: MockMarketSource (детерминированный генератор рынка)">
        <div className="grid gap-3 md:grid-cols-4">
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Блоков</label>
            <input className="field num" type="number" min={1} max={2000} value={blocks} onChange={(e) => setBlocks(Number(e.target.value))} />
          </div>
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Seed</label>
            <input className="field num" type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
          </div>
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Профиль рынка</label>
            <select className="field" value={profile} onChange={(e) => setProfile(e.target.value)}>
              {(profiles.data?.profiles ?? ['calm', 'normal', 'volatile', 'stress']).map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Min profit, $</label>
            <input className="field num" type="number" value={minProfit} onChange={(e) => setMinProfit(Number(e.target.value))} />
          </div>
        </div>

        <div className="mt-4">
          <label className="mb-2 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Стратегии</label>
          <div className="flex flex-wrap gap-2">
            {ALL_STRATEGIES.map((name) => {
              const active = selected.includes(name);
              return (
                <button
                  key={name}
                  className={`btn ${active ? 'btn-tonal' : 'btn-outline'} !py-1.5 text-xs`}
                  onClick={() => setSelected((prev) => (active ? prev.filter((item) => item !== name) : [...prev, name]))}
                >
                  {name}
                </button>
              );
            })}
          </div>
        </div>

        <div className="mt-5 flex items-center gap-3">
          <button className="btn btn-filled" disabled={running || selected.length === 0} onClick={() => void run()}>
            <Play size={15} /> {running ? 'Считаем…' : 'Запустить бэктест'}
          </button>
          <span className="text-xs text-[var(--text-3)]">
            {blocks} блоков × {selected.length} стратегий · ориентировочно {(blocks / 8).toFixed(1)} с
          </span>
        </div>
      </GlassCard>

      {result && (
        <>
          <div className="grid-cards">
            <KpiTile label="Найдено возможностей" value={result.total_found} icon={<FlaskConical size={16} />} tone="primary" />
            <KpiTile
              label="Net PnL прогона"
              value={`$${result.total_net_usd.toLocaleString('ru-RU', { maximumFractionDigits: 2 })}`}
              tone={result.total_net_usd >= 0 ? 'profit' : 'loss'}
              hint={`${result.avg_per_block_usd.toFixed(4)} $/блок`}
            />
            <KpiTile
              label="Скорость"
              value={`${result.blocks_per_sec} бл/с`}
              hint={`длительность ${result.duration_sec.toFixed(2)} с`}
              icon={<Timer size={16} />}
            />
            <KpiTile label="Лучшая стратегия" value={[...result.by_strategy].sort((a, b) => b.net_usd - a.net_usd)[0]?.strategy ?? '—'} hint="по net PnL" />
          </div>

          <GlassCard title="Кривая капитала прогона" subtitle="кумулятивный net по блокам">
            <Chart option={equityOption} height={280} />
          </GlassCard>

          <GlassCard title="Результат по стратегиям" padded={false}>
            <DataTable columns={strategyColumns} rows={result.by_strategy} rowKey={(row) => row.strategy} />
          </GlassCard>

          <GlassCard title="Заметка движка" subtitle="как считаются данные">
            <p className="text-xs text-[var(--text-2)]">{result.note}</p>
          </GlassCard>
        </>
      )}

      {history.length > 0 && (
        <GlassCard title="Сравнение прогонов" subtitle="последние 8 запусков" padded={false}>
          <DataTable columns={runColumns} rows={history}
            rowKey={(row) => row.label}
          />
        </GlassCard>
      )}
    </div>
  );
}
