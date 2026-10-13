/** Кошельки: балансы, ключи (маскируются), refill газа и автосивип прибыли. */
import { useState } from 'react';
import { ArrowDownToLine, KeyRound, Plus, Send, Wallet as WalletIcon } from 'lucide-react';
import { api } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { useApi } from '../hooks/useApi';
import { useToast } from '../components/Toast';

interface WalletRow {
  id: number;
  name: string;
  address: string;
  is_hot: boolean;
  kind: string;
  chains: number[];
  balance_usd: number;
  native_balance: number;
  has_key: boolean;
  hsm_ref: string | null;
}

interface WalletsResponse {
  items: WalletRow[];
  native_price_usd: number;
  sweep: { interval_sec: number; cold_wallet: string | null; min_amount_usd: number };
}

interface SweepPlan {
  accumulated_usd: number;
  threshold_usd: number;
  will_sweep: boolean;
  cold_wallet: string;
  interval_sec: number;
  dry_run: boolean;
}

const money = (value: number): string => `$${value.toLocaleString('ru-RU', { maximumFractionDigits: 2 })}`;

export function Wallets() {
  const wallets = useApi<WalletsResponse>('/wallets', 10000);
  const plan = useApi<SweepPlan>('/wallets/sweep/plan', 15000);
  const { push } = useToast();
  const [keyFor, setKeyFor] = useState<WalletRow | null>(null);
  const [privateKey, setPrivateKey] = useState('');
  const [sweepOpen, setSweepOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newWallet, setNewWallet] = useState({ name: '', address: '', private_key: '', is_hot: true });

  const columns: Column<WalletRow>[] = [
    {
      key: 'name',
      title: 'Кошелёк',
      render: (row) => (
        <div className="flex items-center gap-2">
          <WalletIcon size={15} className="text-[#b9a6ff]" />
          <div>
            <div className="strong text-sm">{row.name}</div>
            <div className="num text-[11px] text-[var(--text-3)]">{row.address}</div>
          </div>
        </div>
      ),
    },
    { key: 'kind', title: 'Тип', render: (row) => <StatusPill status={row.is_hot ? 'degraded' : 'ok'} label={row.is_hot ? 'hot' : 'cold'} /> },
    { key: 'chains', title: 'Сети', render: (row) => <span className="num text-xs">{row.chains.join(', ') || '—'}</span> },
    { key: 'balance', title: 'Баланс', render: (row) => <div><div className="num strong">{money(row.balance_usd)}</div><div className="num text-[11px] text-[var(--text-3)]">{row.native_balance.toFixed(4)} native</div></div> },
    {
      key: 'key',
      title: 'Ключ',
      render: (row) =>
        row.hsm_ref ? (
          <span className="pill pill-info">HSM</span>
        ) : row.has_key ? (
          <span className="pill pill-profit">зашифрован</span>
        ) : (
          <span className="pill">нет</span>
        ),
    },
    {
      key: 'actions',
      title: '',
      render: (row) => (
        <div className="flex gap-2">
          <button className="btn btn-outline !px-3 !py-1.5 text-xs" onClick={() => setKeyFor(row)}>
            <KeyRound size={13} /> Ключ
          </button>
          <button
            className="btn btn-outline !px-3 !py-1.5 text-xs"
            onClick={async () => {
              try {
                const result = await api.post<{ plan: Record<string, unknown> }>(`/wallets/${row.id}/refill`, {
                  chain_id: row.chains[0] ?? 1,
                  amount_native: 0.5,
                });
                push('info', `Refill-план (dry-run: ${String(result.plan.dry_run)}): 0.5 native → ${row.name}`);
              } catch (error) {
                push('error', error instanceof Error ? error.message : 'Ошибка refill');
              }
            }}
          >
            <ArrowDownToLine size={13} /> Refill
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid-cards">
        <GlassCard title="Сивип прибыли" subtitle={`каждые ${plan.data?.interval_sec ?? '—'} с`}>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-[var(--text-2)]">Накоплено</span>
              <span className="num strong">{money(plan.data?.accumulated_usd ?? 0)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[var(--text-2)]">Порог</span>
              <span className="num">{money(plan.data?.threshold_usd ?? 0)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[var(--text-2)]">Холодный кошелёк</span>
              <span className="num text-[11px]">{plan.data?.cold_wallet ?? '—'}</span>
            </div>
            <button className="btn btn-tonal mt-2 w-full" onClick={() => setSweepOpen(true)}>
              <Send size={14} /> Запустить сивип сейчас
            </button>
          </div>
        </GlassCard>

        <GlassCard title="Добавить кошелёк" subtitle="приватный ключ шифруется (AES-подобная схема / Vault)">
          {!creating ? (
            <button className="btn btn-filled" onClick={() => setCreating(true)}>
              <Plus size={15} /> Новый кошелёк
            </button>
          ) : (
            <div className="space-y-3">
              <input className="field" placeholder="Имя (напр. hot-arb-1)" value={newWallet.name} onChange={(e) => setNewWallet({ ...newWallet, name: e.target.value })} />
              <input className="field num" placeholder="0x-адрес" value={newWallet.address} onChange={(e) => setNewWallet({ ...newWallet, address: e.target.value })} />
              <input className="field num" type="password" placeholder="Приватный ключ (0x…)" value={newWallet.private_key} onChange={(e) => setNewWallet({ ...newWallet, private_key: e.target.value })} />
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={newWallet.is_hot} onChange={(e) => setNewWallet({ ...newWallet, is_hot: e.target.checked })} />
                Горячий (для исполнения)
              </label>
              <div className="flex gap-2">
                <button
                  className="btn btn-filled"
                  onClick={async () => {
                    try {
                      await api.post('/wallets', { ...newWallet, chain_scope: [1, 8453] });
                      push('success', 'Кошелёк добавлен');
                      setCreating(false);
                      setNewWallet({ name: '', address: '', private_key: '', is_hot: true });
                      await wallets.refetch();
                    } catch (error) {
                      push('error', error instanceof Error ? error.message : 'Ошибка добавления');
                    }
                  }}
                >
                  Сохранить
                </button>
                <button className="btn btn-outline" onClick={() => setCreating(false)}>Отмена</button>
              </div>
            </div>
          )}
        </GlassCard>

        <GlassCard title="Горячие / холодные" subtitle="разделение ролей">
          <ul className="space-y-2 text-sm text-[var(--text-2)]">
            <li>🔥 Hot — держит рабочий капитал и газ, участвует в исполнении.</li>
            <li>🧊 Cold — хранит основную прибыль, получает автосивип.</li>
            <li>🛡 Ключи hot-кошельков шифруются ключом `SECRET_KEY` или Vault transit.</li>
          </ul>
          <p className="mt-3 text-[11px] text-[var(--text-3)]">
            1 native ≈ {money(wallets.data?.native_price_usd ?? 0)} (по рыночному снимку)
          </p>
        </GlassCard>
      </div>

      <GlassCard title="Кошельки" subtitle="балансы обновляются каждые 10 секунд" padded={false}>
        <DataTable columns={columns} rows={wallets.data?.items ?? []} rowKey={(row) => row.id} loading={wallets.loading} />
      </GlassCard>

      <ConfirmDialog
        open={keyFor !== null}
        title={`Приватный ключ: ${keyFor?.name ?? ''}`}
        description="Ключ будет зашифрован и сохранён. В целях безопасности сам ключ никогда не показывается повторно."
        confirmLabel="Сохранить ключ"
        onCancel={() => {
          setKeyFor(null);
          setPrivateKey('');
        }}
        onConfirm={async () => {
          if (!keyFor) return;
          try {
            const result = await api.post<{ masked: string }>(`/wallets/${keyFor.id}/key`, { private_key: privateKey });
            push('success', `Ключ сохранён (${result.masked})`);
            await wallets.refetch();
          } catch (error) {
            push('error', error instanceof Error ? error.message : 'Ошибка сохранения ключа');
          } finally {
            setKeyFor(null);
            setPrivateKey('');
          }
        }}
      />

      <ConfirmDialog
        open={sweepOpen}
        title="Немедленный сивип прибыли"
        description={`Средства будут переведены на холодный кошелёк ${plan.data?.cold_wallet ?? '(не настроен)'}. В dry-run выполняется только запись в журнал.`}
        confirmLabel="Сивип"
        onCancel={() => setSweepOpen(false)}
        onConfirm={async () => {
          try {
            const result = await api.post<{ result: Record<string, unknown> }>('/wallets/sweep/run');
            push('success', `Сивип: ${JSON.stringify(result.result).slice(0, 120)}`);
            await Promise.all([plan.refetch(), wallets.refetch()]);
          } catch (error) {
            push('error', error instanceof Error ? error.message : 'Ошибка сивипа');
          } finally {
            setSweepOpen(false);
          }
        }}
      />
    </div>
  );
}
