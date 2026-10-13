/** Дашборд: PnL по периодам, кривая капитала, live-возможности, сервисы и релеи. */
import { useMemo } from 'react';
import { Activity, ArrowUpRight, Flame, Percent, Server, TrendingUp, Wallet as WalletIcon } from 'lucide-react';
import { useApi } from '../hooks/useApi';
import { useLive } from '../hooks/useLive';
import type { OpportunityRow, Overview, RelayStat } from '../api/client';
import { Chart, axisStyle, glassTooltip } from '../components/Chart';
import { GlassCard } from '../components/GlassCard';
import { KpiTile } from '../components/KpiTile';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';

const money = (value: number | undefined): string =>
  value === undefined || Number.isNaN(value)
    ? '—'
    : `${value < 0 ? '−' : ''}$${Math.abs(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })}`;

export function Dashboard() {
  const overview = useApi<Overview>('/dashboard/overview', 5000);
  const live = useLive(true);

  const pnl = overview.data?.pnl;
  const live24 = pnl?.['24h'];
  const equityOption = useMemo(() => {
    const curve = overview.data?.equity_curve ?? [];
    return {
      tooltip: { trigger: 'axis' as const, ...glassTooltip },
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      xAxis: {
        type: 'category' as const,
        data: curve.map((point) => point.ts.slice(11, 16)),
        ...axisStyle,
      },
      yAxis: { type: 'value' as const, ...axisStyle },
      series: [
        {
          name: 'Кумулятивный PnL, $',
          type: 'line' as const,
          smooth: true,
          showSymbol: false,
          data: curve.map((point) => point.cumulative_usd.toFixed(2)),
          lineStyle: { width: 2.5, color: '#00ffa3' },
          areaStyle: {
            color: {
              type: 'linear' as const,
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(0,255,163,0.35)' },
                { offset: 1, color: 'rgba(0,255,163,0.02)' },
              ],
            },
          },
        },
      ],
    };
  }, [overview.data]);

  const liveRows = (live.events.filter((event) => event.type === 'opportunity').slice(0, 8).map((event) => event.data) ??
    []) as unknown as OpportunityRow[];

  const opportunityColumns: Column<OpportunityRow>[] = [
    { key: 'strategy', title: 'Стратегия', render: (row) => <span className="pill pill-primary">{row.strategy}</span> },
    { key: 'route', title: 'Маршрут', render: (row) => <span className="num text-xs">{String(row.meta?.route ?? `${row.dex_buy} → ${row.dex_sell}`)}</span> },
    { key: 'size', title: 'Размер', render: (row) => <span className="num">{money(row.size_usd)}</span> },
    {
      key: 'net',
      title: 'Net PnL',
      render: (row) => (
        <span className={`num font-semibold ${row.net_profit_usd >= 0 ? 'profit' : 'loss'}`}>{money(row.net_profit_usd)}</span>
      ),
    },
    { key: 'roi', title: 'ROI', render: (row) => <span className="num">{row.roi_pct.toFixed(3)}%</span> },
  ];

  const relayColumns: Column<RelayStat>[] = [
    {
      key: 'relay',
      title: 'Релей',
      render: (row) => (
        <span className="flex items-center gap-2 text-sm">
          <span className={row.enabled ? 'live-dot' : 'h-2 w-2 rounded-full bg-white/20'} />
          {row.relay}
        </span>
      ),
    },
    { key: 'sent', title: 'Отправлено', render: (row) => <span className="num">{row.sent}</span> },
    {
      key: 'rate',
      title: 'Успех',
      render: (row) => (
        <span className={`num ${row.success_rate_pct >= 80 ? 'profit' : row.success_rate_pct > 0 ? '' : 'text-[var(--text-3)]'}`}>
          {row.success_rate_pct.toFixed(0)}%
        </span>
      ),
    },
  ];

  const serviceRows = useMemo(() => {
    const stats = overview.data?.services.strategy_service;
    const market = overview.data?.services.market;
    return [
      { name: 'strategy-service', value: stats?.avg_latency_ms ?? 0, hint: `${stats?.blocks_processed ?? 0} блоков` },
      { name: 'pool-indexer', value: Number(market?.pools ?? 0), hint: 'пулов в кэше' },
      { name: 'liq-scanner', value: Number(market?.positions ?? 0), hint: 'позиций под наблюдением' },
      { name: 'mempool', value: Number(market?.pending_swaps ?? 0), hint: 'крупных свопов' },
    ];
  }, [overview.data]);

  return (
    <div className="space-y-4">
      <div className="grid-cards">
        <KpiTile
          label="PnL 24 часа"
          value={money(live24?.net_usd)}
          tone={(live24?.net_usd ?? 0) >= 0 ? 'profit' : 'loss'}
          hint={`сделок: ${live24?.trades ?? 0}, win rate ${(live24?.win_rate_pct ?? 0).toFixed(1)}%`}
          icon={<TrendingUp size={16} />}
        />
        <KpiTile
          label="PnL 7 дней"
          value={money(pnl?.['7d']?.net_usd)}
          tone={(pnl?.['7d']?.net_usd ?? 0) >= 0 ? 'profit' : 'loss'}
          hint={`Sharpe ${(pnl?.['7d']?.sharpe ?? 0).toFixed(2)}`}
          icon={<ArrowUpRight size={16} />}
        />
        <KpiTile
          label="PnL за всё время"
          value={money(pnl?.all?.net_usd)}
          tone={(pnl?.all?.net_usd ?? 0) >= 0 ? 'profit' : 'loss'}
          hint={`сделок: ${pnl?.all?.trades ?? 0}, ROI ${(pnl?.all?.roi_pct ?? 0).toFixed(2)}%`}
          icon={<Percent size={16} />}
        />
        <KpiTile
          label="Возможности (сервис)"
          value={overview.data?.services.strategy_service.opportunities_found ?? 0}
          hint={`исполнено: ${overview.data?.services.strategy_service.opportunities_executed ?? 0} · отклонено: ${overview.data?.services.strategy_service.opportunities_rejected ?? 0}`}
          tone="primary"
          icon={<Activity size={16} />}
        />
        <KpiTile
          label="Режим"
          value={overview.data?.dry_run ? 'DRY-RUN' : 'LIVE'}
          tone={overview.data?.dry_run ? 'warn' : 'profit'}
          hint={overview.data?.dry_run ? 'сделки не отправляются в сеть' : 'внимание: реальные средства'}
          icon={<Flame size={16} />}
        />
        <KpiTile
          label="Gas ceiling"
          value={`${overview.data?.risk.gas_ceiling_gwei ?? 0} gwei`}
          hint={`min profit $${overview.data?.risk.min_profit_usd ?? 0} · макс. размер ${money(overview.data?.risk.max_size_usd)}`}
          icon={<Server size={16} />}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <GlassCard title="Кривая капитала (24 ч)" subtitle="кумулятивный net PnL" className="xl:col-span-2">
          <Chart option={equityOption} height={280} />
        </GlassCard>

        <GlassCard title="Кошельки" subtitle="горячие и холодные" actions={<WalletIcon size={16} />}>
          <div className="space-y-3">
            {(overview.data?.wallets ?? []).map((wallet) => (
              <div key={wallet.id} className="glass flex items-center justify-between px-4 py-3">
                <div>
                  <div className="text-sm font-medium">{wallet.name}</div>
                  <div className="num text-[11px] text-[var(--text-3)]">{wallet.address.slice(0, 10)}…{wallet.address.slice(-6)}</div>
                </div>
                <div className="text-right">
                  <div className="num text-sm font-semibold">{money(wallet.balance_usd)}</div>
                  <StatusPill status={wallet.is_hot ? 'degraded' : 'ok'} label={wallet.is_hot ? 'hot' : 'cold'} />
                </div>
              </div>
            ))}
            {!overview.data?.wallets?.length && <p className="text-sm text-[var(--text-3)]">Кошельки не настроены</p>}
          </div>
        </GlassCard>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <GlassCard title="Live-возможности" subtitle="поток из WebSocket" className="xl:col-span-2" padded={false}>
          <DataTable
            columns={opportunityColumns}
            rows={liveRows}
            rowKey={(row, index) => row.uid ?? `live-${index}`}
            empty="Ожидание потока возможностей…"
            maxHeight={300}
          />
        </GlassCard>

        <div className="space-y-4">
          <GlassCard title="Сервисы" subtitle="нагрузка и состояние">
            <div className="space-y-2">
              {serviceRows.map((service) => (
                <div key={service.name} className="flex items-center justify-between text-sm">
                  <span className="text-[var(--text-2)]">{service.name}</span>
                  <span className="num">
                    {typeof service.value === 'number' ? service.value.toFixed(1) : service.value}
                    <span className="ml-2 text-[11px] text-[var(--text-3)]">{service.hint}</span>
                  </span>
                </div>
              ))}
            </div>
          </GlassCard>

          <GlassCard title="Релеи" subtitle="отправка bundle" padded={false}>
            <DataTable
              columns={relayColumns}
              rows={overview.data?.services.relays ?? []}
              rowKey={(row) => row.relay}
              maxHeight={240}
            />
          </GlassCard>
        </div>
      </div>
    </div>
  );
}
