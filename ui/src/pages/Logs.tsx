/** Логи: живой поток событий (WS) и журнал аудита оператора. */
import { useMemo, useState } from 'react';
import { Pause, Play, ScrollText, Search } from 'lucide-react';
import { GlassCard } from '../components/GlassCard';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';
import { useApi } from '../hooks/useApi';
import { useLive } from '../hooks/useLive';

interface AuditRow {
  id: number;
  ts: string;
  username: string;
  action: string;
  payload: Record<string, unknown>;
  ip: string;
}

const LEVEL_STYLE: Record<string, string> = {
  opportunity: 'text-[var(--accent-info)]',
  execution: 'text-[var(--accent-profit)]',
  kill_switch: 'text-[var(--accent-loss)]',
  mode: 'text-[var(--accent-warn)]',
  sweep: 'text-[#b9a6ff]',
};

export function Logs() {
  const live = useLive(true);
  const audit = useApi<{ items: AuditRow[] }>('/logs/audit?page=1&page_size=60', 10000);
  const server = useApi<{ items: { ts: string; type: string; data: Record<string, unknown> }[] }>('/logs/events?limit=200', 0);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState('');
  const [tab, setTab] = useState<'live' | 'audit'>('live');

  const rows = useMemo(() => {
    const source = server.data?.items ?? [];
    const merged = [...source];
    if (!paused) {
      for (const event of live.events) {
        if (event.type === 'state') continue;
        merged.unshift({ ts: new Date((event.ts ?? Date.now() / 1000) * 1000).toISOString(), type: event.type, data: event.data });
      }
    }
    return merged
      .filter((row) => !filter || JSON.stringify(row).toLowerCase().includes(filter.toLowerCase()))
      .slice(0, 300);
  }, [server.data, live.events, paused, filter]);

  const auditColumns: Column<AuditRow>[] = [
    { key: 'ts', title: 'Время', render: (row) => <span className="num text-xs">{row.ts.replace('T', ' ').slice(0, 19)}</span> },
    { key: 'user', title: 'Оператор', render: (row) => <span className="pill pill-info">{row.username}</span> },
    { key: 'action', title: 'Действие', render: (row) => <span className="num text-xs">{row.action}</span> },
    {
      key: 'payload',
      title: 'Payload',
      render: (row) => <span className="num text-[11px] text-[var(--text-3)]">{JSON.stringify(row.payload).slice(0, 140)}</span>,
    },
    { key: 'ip', title: 'IP', render: (row) => <span className="num text-[11px]">{row.ip}</span> },
  ];

  return (
    <div className="space-y-4">
      <GlassCard
        title="Журнал событий"
        subtitle={tab === 'live' ? `${rows.length} строк · поток WS ${live.connected ? 'активен' : 'недоступен'}` : 'действия оператора (аудит)'}
        actions={
          <>
            <button className={`btn ${tab === 'live' ? 'btn-filled' : 'btn-outline'}`} onClick={() => setTab('live')}>
              <ScrollText size={14} /> Live
            </button>
            <button className={`btn ${tab === 'audit' ? 'btn-filled' : 'btn-outline'}`} onClick={() => setTab('audit')}>
              Аудит
            </button>
            {tab === 'live' && (
              <button className="btn btn-outline" onClick={() => setPaused((value) => !value)}>
                {paused ? <Play size={14} /> : <Pause size={14} />} {paused ? 'Продолжить' : 'Пауза'}
              </button>
            )}
          </>
        }
        padded={false}
      >
        {tab === 'live' ? (
          <>
            <div className="flex items-center gap-2 px-5 py-4">
              <Search size={15} className="text-[var(--text-3)]" />
              <input
                className="field"
                placeholder="Фильтр по тексту события (напр. dex_arb, kill_switch, блок)"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
              />
            </div>
            <div className="max-h-[560px] overflow-auto font-mono text-[12px] leading-relaxed">
              {rows.length === 0 && <div className="py-10 text-center text-[var(--text-3)]">Событий пока нет</div>}
              {rows.map((row, index) => (
                <div key={`${row.ts}-${index}`} className="flex gap-3 border-b border-white/5 px-5 py-1.5 hover:bg-[var(--state-hover)]">
                  <span className="num shrink-0 text-[var(--text-3)]">{row.ts.replace('T', ' ').slice(11, 19)}</span>
                  <span className={`w-28 shrink-0 font-semibold ${LEVEL_STYLE[row.type] ?? 'text-[var(--text-2)]'}`}>{row.type}</span>
                  <span className="num break-all text-[var(--text-2)]">{JSON.stringify(row.data)}</span>
                </div>
              ))}
            </div>
          </>
        ) : (
          <DataTable columns={auditColumns} rows={audit.data?.items ?? []} rowKey={(row) => row.id} empty="Записей аудита нет" maxHeight={560} />
        )}
      </GlassCard>

      <GlassCard title="Состояние пайплайна" subtitle="ключевые счётчики ончейн-части">
        <div className="grid-cards">
          {[
            { label: 'События (буфер)', value: live.events.length },
            { label: 'WS', value: live.connected ? 'online' : 'offline' },
            { label: 'Блок', value: (live.state as { market?: { block_number?: number } } | null)?.market?.block_number ?? '—' },
            { label: 'Стратегии активны', value: (live.state as { strategies_running?: string[] } | null)?.strategies_running?.join(', ') ?? '—' },
          ].map((item) => (
            <div key={item.label} className="glass px-4 py-3">
              <div className="text-[11px] uppercase tracking-wider text-[var(--text-3)]">{item.label}</div>
              <div className="num mt-1 text-sm font-semibold">{String(item.value)}</div>
            </div>
          ))}
        </div>
        <div className="mt-3">
          <StatusPill status={live.connected ? 'ok' : 'failed'} label={live.connected ? 'WS подключён' : 'WS отключён'} />
        </div>
      </GlassCard>
    </div>
  );
}
