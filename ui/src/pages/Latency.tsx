/** Латентность: бюджеты по сервисам (ТЗ §1.2), гистограмма, живой p99. */
import { useMemo, useState } from 'react';
import { Gauge, Timer, TriangleAlert } from 'lucide-react';
import { GlassCard } from '../components/GlassCard';
import { KpiTile } from '../components/KpiTile';
import { Chart, axisStyle, glassTooltip } from '../components/Chart';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';
import { useApi } from '../hooks/useApi';

interface LatencyResponse {
  budgets_us: Record<string, number>;
  services: { service: string; samples: number; p50_us: number; p95_us: number; p99_us: number; budget_us: number; within_budget: boolean }[];
  history: { ts: string; service: string; metric: string; p50_us: number; p95_us: number; p99_us: number; count: number }[];
  live: { avg_block_latency_ms: number; buffer_size: number; risk_p99_ms: number };
}

interface Histogram {
  buckets: { from_ms: number; to_ms: number; count: number }[];
  max_ms: number;
  samples: number;
}

export function Latency() {
  const [service, setService] = useState('');
  const { data } = useApi<LatencyResponse>(`/latency/metrics${service ? `?service=${service}` : ''}`, 10000);
  const histogram = useApi<Histogram>('/latency/histogram?buckets=16', 15000);

  const budgetOption = useMemo(() => {
    const rows = data?.services ?? [];
    return {
      tooltip: { trigger: 'axis' as const, ...glassTooltip },
      legend: { bottom: 0, textStyle: { color: 'rgba(238,241,255,0.7)' } },
      grid: { left: 8, right: 16, top: 24, bottom: 32, containLabel: true },
      xAxis: { type: 'category' as const, data: rows.map((row) => row.service), ...axisStyle },
      yAxis: { type: 'value' as const, name: 'мс', ...axisStyle },
      series: [
        {
          name: 'p99, мс',
          type: 'bar' as const,
          data: rows.map((row) => ({
            value: (row.p99_us / 1000).toFixed(3),
            itemStyle: { color: row.within_budget ? 'rgba(0,255,163,0.8)' : 'rgba(255,77,109,0.85)', borderRadius: [6, 6, 0, 0] },
          })),
        },
        {
          name: 'Бюджет, мс',
          type: 'line' as const,
          data: rows.map((row) => (row.budget_us / 1000).toFixed(3)),
          lineStyle: { color: '#7c5cff', type: 'dashed' as const, width: 2 },
          showSymbol: false,
        },
      ],
    };
  }, [data]);

  const histogramOption = useMemo(
    () => ({
      tooltip: { trigger: 'axis' as const, ...glassTooltip },
      grid: { left: 8, right: 16, top: 20, bottom: 8, containLabel: true },
      xAxis: {
        type: 'category' as const,
        data: (histogram.data?.buckets ?? []).map((bucket) => `${bucket.from_ms.toFixed(1)}`),
        ...axisStyle,
      },
      yAxis: { type: 'value' as const, ...axisStyle },
      series: [
        {
          name: 'Сделок в интервале',
          type: 'bar' as const,
          data: (histogram.data?.buckets ?? []).map((bucket) => bucket.count),
          itemStyle: { color: 'rgba(77,201,255,0.8)', borderRadius: [6, 6, 0, 0] },
        },
      ],
    }),
    [histogram.data],
  );

  const columns: Column<LatencyResponse['services'][number]>[] = [
    { key: 'service', title: 'Сервис', render: (row) => <span className="strong text-sm">{row.service}</span> },
    { key: 'samples', title: 'Замеров', render: (row) => <span className="num">{row.samples}</span> },
    { key: 'p50', title: 'p50', render: (row) => <span className="num text-xs">{(row.p50_us / 1000).toFixed(3)} мс</span> },
    { key: 'p95', title: 'p95', render: (row) => <span className="num text-xs">{(row.p95_us / 1000).toFixed(3)} мс</span> },
    {
      key: 'p99',
      title: 'p99 / бюджет',
      render: (row) => (
        <span className={`num text-xs ${row.within_budget ? 'profit' : 'loss'}`}>
          {(row.p99_us / 1000).toFixed(3)} / {(row.budget_us / 1000).toFixed(3)} мс
        </span>
      ),
    },
    { key: 'ok', title: 'В бюджете', render: (row) => <StatusPill status={row.within_budget ? 'ok' : 'failed'} label={row.within_budget ? 'да' : 'превышен'} /> },
  ];

  return (
    <div className="space-y-4">
      <div className="grid-cards">
        <KpiTile label="Средняя латентность блока" value={`${(data?.live.avg_block_latency_ms ?? 0).toFixed(1)} мс`} icon={<Timer size={16} />} tone={(data?.live.avg_block_latency_ms ?? 0) < 200 ? 'profit' : 'warn'} hint="бюджет блока: 200 мс (§1.2)" />
        <KpiTile label="Risk p99" value={`${(data?.live.risk_p99_ms ?? 0).toFixed(2)} мс`} icon={<Gauge size={16} />} hint="лимит риск-движка: 1 мс" />
        <KpiTile label="Замеров в БД" value={data?.history.length ?? 0} hint="последние 200 записей" />
        <KpiTile label="Гистограмма: выборка" value={histogram.data?.samples ?? 0} hint={`максимум ${(histogram.data?.max_ms ?? 0).toFixed(1)} мс`} icon={<TriangleAlert size={16} />} />
      </div>

      <GlassCard
        title="Бюджеты сервисов"
        subtitle="p99 из БД против бюджета ТЗ §1.2"
        actions={
          <select className="field !w-48" value={service} onChange={(event) => setService(event.target.value)}>
            <option value="">все сервисы</option>
            {(data?.services ?? []).map((row) => (
              <option key={row.service} value={row.service}>{row.service}</option>
            ))}
          </select>
        }
      >
        <Chart option={budgetOption} height={300} />
      </GlassCard>

      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard title="Гистограмма задержек" subtitle="распределение p99 по сделкам">
          <Chart option={histogramOption} height={260} />
        </GlassCard>
        <GlassCard title="Сервисы" padded={false}>
          <DataTable columns={columns} rows={data?.services ?? []} rowKey={(row) => row.service} empty="Замеров пока нет — запустите оркестратор" maxHeight={300} />
        </GlassCard>
      </div>
    </div>
  );
}
