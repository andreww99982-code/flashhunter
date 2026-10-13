/** Live-таблица возможностей: фильтры, детали, ручное исполнение (с подтверждением). */
import { useMemo, useState } from 'react';
import { Filter, Play, RefreshCw, Zap } from 'lucide-react';
import { api, type OpportunityRow } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { useLive } from '../hooks/useLive';
import { useToast } from '../components/Toast';
import { useApi } from '../hooks/useApi';

const money = (value: number): string => `${value < 0 ? '−' : ''}$${Math.abs(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })}`;

interface DbRow {
  id: number;
  ts: string;
  strategy: string;
  chain_id: number;
  dex_buy: string;
  dex_sell: string;
  size_usd: number;
  net_profit_usd: number;
  roi_pct: number;
  confidence: number;
  status: string;
  block_number: number | null;
  path?: Record<string, unknown>;
}

export function Opportunities() {
  const live = useLive(true);
  const { push } = useToast();
  const [strategy, setStrategy] = useState('');
  const [minProfit, setMinProfit] = useState(0);
  const [mode, setMode] = useState<'live' | 'db'>('live');
  const [selected, setSelected] = useState<OpportunityRow | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const dbQuery = `/opportunities?limit=100${strategy ? `&strategy=${strategy}` : ''}${minProfit ? `&min_profit=${minProfit}` : ''}`;
  const db = useApi<{ items: DbRow[]; total: number }>(dbQuery, mode === 'db' ? 5000 : 0, mode === 'db');

  const liveRows = useMemo(() => {
    const rows: OpportunityRow[] = [];
    for (const event of live.events) {
      if (event.type !== 'opportunity') continue;
      const row = event.data as unknown as OpportunityRow;
      if (strategy && row.strategy !== strategy) continue;
      if (row.net_profit_usd < minProfit) continue;
      rows.push(row);
      if (rows.length >= 120) break;
    }
    // перезапуск одинаковых связок — оставляем самые свежие
    const seen = new Set<string>();
    return rows.filter((row) => {
      const key = `${row.strategy}:${row.pools?.join(',')}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [live.events, strategy, minProfit]);

  const columns: Column<OpportunityRow>[] = [
    {
      key: 'strategy',
      title: 'Стратегия',
      render: (row) => (
        <div className="flex flex-col">
          <span className="pill pill-primary w-fit">{row.strategy}</span>
          <span className="mt-1 text-[11px] text-[var(--text-3)]">{row.kind}</span>
        </div>
      ),
    },
    {
      key: 'route',
      title: 'Маршрут',
      render: (row) => (
        <div className="text-xs">
          <div className="num">{String(row.meta?.route ?? `${row.dex_buy} → ${row.dex_sell}`)}</div>
          <div className="num text-[11px] text-[var(--text-3)]">
            {row.token_in?.slice(0, 8)}… → {row.token_out?.slice(0, 8)}…
          </div>
        </div>
      ),
    },
    { key: 'size', title: 'Размер', render: (row) => <span className="num">{money(row.size_usd)}</span> },
    {
      key: 'gross',
      title: 'Gross / Gas / Fee',
      render: (row) => (
        <div className="num text-xs">
          <div className="profit">{money(row.gross_profit_usd)}</div>
          <div className="text-[var(--text-3)]">
            gas {money(row.gas_usd)} · flash {money(row.flash_fee_usd)}
          </div>
        </div>
      ),
    },
    {
      key: 'net',
      title: 'Net PnL',
      render: (row) => (
        <div>
          <div className={`num font-semibold ${row.net_profit_usd >= 0 ? 'profit' : 'loss'}`}>{money(row.net_profit_usd)}</div>
          <div className="num text-[11px] text-[var(--text-3)]">ROI {row.roi_pct.toFixed(3)}%</div>
        </div>
      ),
    },
    {
      key: 'confidence',
      title: 'Confidence',
      render: (row) => (
        <div className="w-24">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/10">
            <div
              className="h-full rounded-full bg-gradient-to-r from-[#7c5cff] to-[#00ffa3]"
              style={{ width: `${Math.min(100, row.confidence * 100)}%` }}
            />
          </div>
          <div className="num mt-1 text-[11px] text-[var(--text-3)]">{(row.confidence * 100).toFixed(0)}%</div>
        </div>
      ),
    },
    { key: 'block', title: 'Блок', render: (row) => <span className="num text-xs">{row.block_number}</span> },
    {
      key: 'actions',
      title: '',
      render: (row) => (
        <button
          className="btn btn-tonal !px-3 !py-1.5 text-xs"
          onClick={(event) => {
            event.stopPropagation();
            setSelected(row);
            setConfirmOpen(true);
          }}
        >
          <Play size={13} /> Execute
        </button>
      ),
    },
  ];

  const dbColumns: Column<DbRow>[] = [
    { key: 'ts', title: 'Время', render: (row) => <span className="num text-xs">{row.ts.replace('T', ' ').slice(0, 19)}</span> },
    { key: 'strategy', title: 'Стратегия', render: (row) => <span className="pill pill-info">{row.strategy}</span> },
    { key: 'dex', title: 'Buy / Sell', render: (row) => <span className="num text-xs">{row.dex_buy} → {row.dex_sell}</span> },
    { key: 'size', title: 'Размер', render: (row) => <span className="num">{money(row.size_usd)}</span> },
    {
      key: 'net',
      title: 'Net PnL',
      render: (row) => <span className={`num font-semibold ${row.net_profit_usd >= 0 ? 'profit' : 'loss'}`}>{money(row.net_profit_usd)}</span>,
    },
    { key: 'status', title: 'Статус', render: (row) => <StatusPill status={row.status} /> },
  ];

  const execute = async () => {
    if (!selected) return;
    try {
      const result = await api.post<{ ok: boolean; result: { status: string; net_profit_usd: number; reason: string } }>(
        '/opportunities/execute',
        { uid: selected.uid },
      );
      if (result.ok) push('success', `Сделка принята: net $${result.result.net_profit_usd.toFixed(2)}`);
      else push('warning', `Отклонено: ${result.result.reason || result.result.status}`);
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Ошибка исполнения');
    } finally {
      setConfirmOpen(false);
    }
  };

  return (
    <div className="space-y-4">
      <GlassCard
        title="Возможности"
        subtitle={`${mode === 'live' ? liveRows.length : db.data?.total ?? 0} записей · политика: без sandwich`}
        actions={
          <>
            <button className={`btn ${mode === 'live' ? 'btn-filled' : 'btn-outline'}`} onClick={() => setMode('live')}>
              <Zap size={14} /> Live
            </button>
            <button className={`btn ${mode === 'db' ? 'btn-filled' : 'btn-outline'}`} onClick={() => setMode('db')}>
              <RefreshCw size={14} /> История
            </button>
          </>
        }
        padded={false}
      >
        <div className="flex flex-wrap items-end gap-3 px-5 py-4">
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">
              <Filter size={11} className="mr-1 inline" /> Стратегия
            </label>
            <select className="field !w-44" value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              <option value="">все</option>
              {['dex_arb', 'triangular', 'stable_peg', 'liquidation', 'jit', 'backrun', 'intent'].map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Min net profit, $</label>
            <input
              className="field !w-32"
              type="number"
              min={0}
              value={minProfit}
              onChange={(event) => setMinProfit(Number(event.target.value))}
            />
          </div>
        </div>

        {mode === 'live' ? (
          <DataTable
            columns={columns}
            rows={liveRows}
            rowKey={(row, index) => row.uid ?? `opp-${index}`}
            empty={live.connected ? 'Пока нет возможностей — ждём блоки…' : 'Нет соединения с оркестратором'}
            maxHeight={560}
          />
        ) : (
          <DataTable
            columns={dbColumns}
            rows={db.data?.items ?? []}
            loading={db.loading}
            rowKey={(row) => row.id}
            maxHeight={560}
          />
        )}
      </GlassCard>

      <ConfirmDialog
        open={confirmOpen}
        title="Ручное исполнение возможности"
        description={
          selected
            ? `${selected.strategy}: ${selected.meta?.route ?? `${selected.dex_buy} → ${selected.dex_sell}`}, размер ${money(selected.size_usd)}, прогноз net ${money(selected.net_profit_usd)}. Возможность будет перепроверена симулятором и риск-движком перед отправкой.`
            : ''
        }
        confirmLabel="Исполнить"
        onCancel={() => setConfirmOpen(false)}
        onConfirm={execute}
      />
    </div>
  );
}
