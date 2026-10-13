/** История сделок: фильтры, разбор revert-причин, экспорт CSV. */
import { useMemo, useState } from 'react';
import { Download, FileWarning, History as HistoryIcon } from 'lucide-react';
import { api } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';
import { Chart, axisStyle, glassTooltip } from '../components/Chart';
import { useApi } from '../hooks/useApi';
import { useToast } from '../components/Toast';

interface TradeRow {
  id: number;
  ts: string;
  chain_id: number;
  strategy: string;
  tx_hash: string | null;
  bundle_hash: string;
  relay: string;
  block_number: number | null;
  gas_used: number;
  gas_price_gwei: number;
  priority_fee_gwei: number;
  actual_profit_usd: number;
  status: string;
  revert_reason: string | null;
  latency_ms: number;
}

export function History() {
  const [strategy, setStrategy] = useState('');
  const [status, setStatus] = useState('');
  const [chainId, setChainId] = useState('');
  const [page, setPage] = useState(1);
  const { push } = useToast();

  const query = useMemo(() => {
    const params = new URLSearchParams({ page: String(page), page_size: '25' });
    if (strategy) params.set('strategy', strategy);
    if (status) params.set('status', status);
    if (chainId) params.set('chain_id', chainId);
    return `/history?${params.toString()}`;
  }, [strategy, status, chainId, page]);

  const { data, loading } = useApi<{ items: TradeRow[]; total: number }>(query, 8000);
  const rows = data?.items ?? [];
  const pages = Math.max(1, Math.ceil((data?.total ?? 0) / 25));

  const pnlOption = useMemo(
    () => ({
      tooltip: { trigger: 'axis' as const, ...glassTooltip },
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      xAxis: { type: 'category' as const, data: rows.map((row) => `#${row.id}`), ...axisStyle },
      yAxis: { type: 'value' as const, ...axisStyle },
      series: [
        {
          name: 'Фактическая прибыль, $',
          type: 'bar' as const,
          data: rows.map((row) => ({
            value: row.actual_profit_usd.toFixed(2),
            itemStyle: { color: row.actual_profit_usd >= 0 ? 'rgba(0,255,163,0.75)' : 'rgba(255,77,109,0.75)', borderRadius: [6, 6, 0, 0] },
          })),
        },
      ],
    }),
    [rows],
  );

  const columns: Column<TradeRow>[] = [
    { key: 'ts', title: 'Время', render: (row) => <span className="num text-xs">{row.ts.replace('T', ' ').slice(0, 19)}</span> },
    { key: 'strategy', title: 'Стратегия', render: (row) => <span className="pill pill-primary">{row.strategy}</span> },
    { key: 'chain', title: 'Сеть', render: (row) => <span className="num text-xs">#{row.chain_id}</span> },
    {
      key: 'hash',
      title: 'Bundle / Tx',
      render: (row) => (
        <span className="num text-[11px] text-[var(--text-3)]" title={`${row.bundle_hash}\n${row.tx_hash ?? ''}`}>
          {row.bundle_hash?.slice(0, 14)}…
          {row.tx_hash ? ` · ${row.tx_hash.slice(0, 10)}…` : ''}
        </span>
      ),
    },
    { key: 'relay', title: 'Релей', render: (row) => <span className="text-xs">{row.relay}</span> },
    {
      key: 'gas',
      title: 'Газ',
      render: (row) => (
        <span className="num text-xs">
          {(row.gas_used / 1000).toFixed(0)}k @ {row.gas_price_gwei.toFixed(1)}
        </span>
      ),
    },
    {
      key: 'profit',
      title: 'Факт. прибыль',
      render: (row) => <span className={`num font-semibold ${row.actual_profit_usd >= 0 ? 'profit' : 'loss'}`}>${row.actual_profit_usd.toFixed(2)}</span>,
    },
    { key: 'latency', title: 'Latency', render: (row) => <span className="num text-xs">{row.latency_ms.toFixed(1)} мс</span> },
    {
      key: 'status',
      title: 'Статус',
      render: (row) => (
        <div className="flex flex-col gap-1">
          <StatusPill status={row.status} />
          {row.revert_reason && (
            <span className="pill pill-loss !text-[10px]" title={row.revert_reason}>
              <FileWarning size={10} /> {row.revert_reason.slice(0, 22)}
            </span>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <GlassCard
        title="История сделок"
        subtitle={`${data?.total ?? 0} записей · фильтры применяются на сервере`}
        actions={
          <button
            className="btn btn-outline"
            onClick={async () => {
              try {
                await api.download('/history/export/csv', `flashhunter-trades-${Date.now()}.csv`);
                push('success', 'CSV выгружен');
              } catch (error) {
                push('error', error instanceof Error ? error.message : 'Ошибка экспорта');
              }
            }}
          >
            <Download size={15} /> Экспорт CSV
          </button>
        }
        padded={false}
      >
        <div className="flex flex-wrap items-end gap-3 px-5 py-4">
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Стратегия</label>
            <select className="field !w-40" value={strategy} onChange={(e) => { setStrategy(e.target.value); setPage(1); }}>
              <option value="">все</option>
              {['dex_arb', 'triangular', 'stable_peg', 'liquidation', 'jit', 'backrun', 'intent'].map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Статус</label>
            <select className="field !w-36" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
              <option value="">все</option>
              {['success', 'reverted', 'rejected', 'dropped', 'failed', 'simulated', 'detected'].map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Сеть</label>
            <select className="field !w-32" value={chainId} onChange={(e) => { setChainId(e.target.value); setPage(1); }}>
              <option value="">все</option>
              {[1, 10, 56, 137, 8453, 42161, 43114].map((id) => (
                <option key={id} value={id}>#{id}</option>
              ))}
            </select>
          </div>
          <div className="ml-auto flex items-center gap-2 text-sm">
            <button className="btn btn-outline !px-3" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>←</button>
            <span className="num">{page} / {pages}</span>
            <button className="btn btn-outline !px-3" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>→</button>
          </div>
        </div>
        <DataTable
          columns={columns}
          rows={rows}
          loading={loading}
          rowKey={(row) => row.id}
          empty={<span className="flex items-center justify-center gap-2"><HistoryIcon size={15} /> Сделок пока нет</span>}
          maxHeight={520}
        />
      </GlassCard>

      <GlassCard title="Прибыль по последним сделкам" subtitle="страница текущего фильтра">
        <Chart option={pnlOption} height={220} />
      </GlassCard>
    </div>
  );
}
