/** Аналитика: PnL по стратегиям/релеям/сетям, топ-пары, дневная разбивка. */
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { KpiTile } from '../components/KpiTile';
import { Chart, axisStyle, glassTooltip } from '../components/Chart';
import { DataTable, type Column } from '../components/DataTable';
import { useApi } from '../hooks/useApi';
import { useToast } from '../components/Toast';
import type { PnlSummary, RelayStat } from '../api/client';

interface AnalyticsResponse {
  period: string;
  pnl: PnlSummary;
  equity_curve: { ts: string; pnl_usd: number; cumulative_usd: number }[];
  by_strategy: { strategy: string; trades: number; net_usd: number; wins: number; losses: number; gas_usd: number }[];
  by_relay: { relay: string; trades: number; net_usd: number }[];
  by_chain: { chain_id: number; trades: number; net_usd: number }[];
  top_pairs: { chain_id: number; strategy: string; trades: number; net_usd: number }[];
  daily: { date: string; chain_id: number; strategy: string; net_usd: number; trades: number; wins: number; losses: number }[];
  relay_stats: RelayStat[];
}

type Period = '24h' | '7d' | '30d' | 'all';

const money = (value: number | undefined): string =>
  value === undefined ? '—' : `${value < 0 ? '−' : ''}$${Math.abs(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })}`;

export function Analytics() {
  const [period, setPeriod] = useState<Period>('7d');
  const { data, refetch } = useApi<AnalyticsResponse>(`/analytics/summary?period=${period}`, 15000);
  const { push } = useToast();

  const strategyOption = useMemo(
    () => ({
      tooltip: { trigger: 'item' as const, ...glassTooltip },
      legend: { bottom: 0, textStyle: { color: 'rgba(238,241,255,0.7)' } },
      series: [
        {
          name: 'Net PnL по стратегиям',
          type: 'pie' as const,
          radius: ['48%', '72%'],
          itemStyle: { borderColor: 'rgba(10,14,39,0.6)', borderWidth: 2, borderRadius: 8 },
          label: { color: 'rgba(238,241,255,0.8)', formatter: '{b}\n{c}$' },
          data: (data?.by_strategy ?? []).map((row, index) => ({
            name: row.strategy,
            value: Number(row.net_usd.toFixed(2)),
            itemStyle: { color: ['#7c5cff', '#00ffa3', '#4dc9ff', '#ffb547', '#ff4d6d', '#a78bfa', '#22d3ee'][index % 7] },
          })),
        },
      ],
    }),
    [data],
  );

  const chainOption = useMemo(
    () => ({
      tooltip: { trigger: 'axis' as const, ...glassTooltip },
      grid: { left: 8, right: 16, top: 20, bottom: 8, containLabel: true },
      xAxis: { type: 'category' as const, data: (data?.by_chain ?? []).map((row) => `#${row.chain_id}`), ...axisStyle },
      yAxis: { type: 'value' as const, ...axisStyle },
      series: [
        {
          name: 'Net PnL по сетям',
          type: 'bar' as const,
          data: (data?.by_chain ?? []).map((row) => row.net_usd.toFixed(2)),
          itemStyle: { color: 'rgba(124,92,255,0.8)', borderRadius: [6, 6, 0, 0] },
        },
      ],
    }),
    [data],
  );

  const pairColumns: Column<AnalyticsResponse['top_pairs'][number]>[] = [
    { key: 'strategy', title: 'Стратегия', render: (row) => <span className="pill pill-primary">{row.strategy}</span> },
    { key: 'chain', title: 'Сеть', render: (row) => <span className="num text-xs">#{row.chain_id}</span> },
    { key: 'trades', title: 'Сделок', render: (row) => <span className="num">{row.trades}</span> },
    {
      key: 'net',
      title: 'Net PnL',
      render: (row) => <span className={`num font-semibold ${row.net_usd >= 0 ? 'profit' : 'loss'}`}>{money(row.net_usd)}</span>,
    },
  ];

  return (
    <div className="space-y-4">
      <GlassCard
        title="Аналитика результата"
        subtitle={`период: ${period}`}
        actions={
          <>
            {(['24h', '7d', '30d', 'all'] as Period[]).map((item) => (
              <button key={item} className={`btn ${period === item ? 'btn-filled' : 'btn-outline'}`} onClick={() => setPeriod(item)}>
                {item}
              </button>
            ))}
            <button
              className="btn btn-tonal"
              onClick={async () => {
                try {
                  const result = await api.post<{ rows: number }>('/analytics/rollup');
                  push('success', `Дневной rollup обновлён: ${result.rows} строк`);
                  await refetch();
                } catch (error) {
                  push('error', error instanceof Error ? error.message : 'Ошибка rollup');
                }
              }}
            >
              Rollup
            </button>
          </>
        }
      >
        <div className="grid-cards">
          <KpiTile label="Net PnL" value={money(data?.pnl.net_usd)} tone={(data?.pnl.net_usd ?? 0) >= 0 ? 'profit' : 'loss'} hint={`gross ${money(data?.pnl.gross_usd)} − газ ${money(data?.pnl.gas_usd)}`} />
          <KpiTile label="Сделок" value={data?.pnl.trades ?? 0} hint={`прибыльных ${data?.pnl.wins ?? 0} / убыточных ${data?.pnl.losses ?? 0}`} />
          <KpiTile label="Win rate" value={`${(data?.pnl.win_rate_pct ?? 0).toFixed(1)}%`} tone="primary" hint={`avg ${money(data?.pnl.avg_profit_usd)}`} />
          <KpiTile label="Sharpe" value={(data?.pnl.sharpe ?? 0).toFixed(2)} hint={`max drawdown ${money(data?.pnl.max_drawdown_usd)}`} />
          <KpiTile label="Лучшая сделка" value={money(data?.pnl.best_usd)} tone="profit" hint={`худшая ${money(data?.pnl.worst_usd)}`} />
          <KpiTile label="ROI капитала" value={`${(data?.pnl.roi_pct ?? 0).toFixed(2)}%`} hint="относительно рабочего капитала" />
        </div>
      </GlassCard>

      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard title="Распределение прибыли" subtitle="по стратегиям">
          <Chart option={strategyOption} height={300} />
        </GlassCard>
        <GlassCard title="PnL по сетям" subtitle="где деньги действительно зарабатываются">
          <Chart option={chainOption} height={300} />
        </GlassCard>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard title="Топ-пары (сеть + стратегия)" padded={false}>
          <DataTable columns={pairColumns} rows={data?.top_pairs ?? []} rowKey={(row) => `${row.chain_id}-${row.strategy}`} />
        </GlassCard>
        <GlassCard title="Релеи" subtitle="конверсия принятия bundle" padded={false}>
          <DataTable
            columns={[
              { key: 'relay', title: 'Релей', render: (row: RelayStat) => <span className="text-sm">{row.relay}</span> },
              { key: 'sent', title: 'Sent', render: (row: RelayStat) => <span className="num">{row.sent}</span> },
              { key: 'ok', title: 'Accepted', render: (row: RelayStat) => <span className="num profit">{row.accepted}</span> },
              { key: 'fail', title: 'Failed', render: (row: RelayStat) => <span className="num loss">{row.failed}</span> },
              { key: 'lat', title: 'Latency', render: (row: RelayStat) => <span className="num">{row.avg_latency_ms.toFixed(1)} мс</span> },
            ]}
            rows={data?.relay_stats ?? []}
            rowKey={(row) => row.relay}
          />
        </GlassCard>
      </div>
    </div>
  );
}
