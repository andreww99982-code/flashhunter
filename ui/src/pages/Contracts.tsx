/** Контракты: реестр, wizard автодеплоя, верификация в эксплорерах. */
import { useState } from 'react';
import { BadgeCheck, Boxes, Rocket, ShieldQuestion } from 'lucide-react';
import { api } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';
import { useApi } from '../hooks/useApi';
import { useToast } from '../components/Toast';

interface ContractRow {
  id: number;
  name: string;
  address: string;
  network: string;
  chain_id: number;
  status: string;
  verified: boolean;
  deployed_at: string | null;
  explorer_url: string | null;
}

const CHAINS = [
  { id: 1, name: 'Ethereum' },
  { id: 56, name: 'BNB Chain' },
  { id: 42161, name: 'Arbitrum One' },
  { id: 8453, name: 'Base' },
  { id: 137, name: 'Polygon' },
  { id: 10, name: 'Optimism' },
  { id: 43114, name: 'Avalanche' },
];

export function Contracts() {
  const { data, refetch } = useApi<{ items: ContractRow[]; wizard: { contracts: string[]; steps: string[] } }>(
    '/contracts',
    10000,
  );
  const { push } = useToast();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [contract, setContract] = useState('FlashLoanArbitrage');
  const [chainId, setChainId] = useState(1);
  const [defiPool, setDefiPool] = useState('0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc');
  const [broadcast, setBroadcast] = useState(false);
  const [busy, setBusy] = useState(false);

  const deploy = async () => {
    setBusy(true);
    try {
      const result = await api.post<{ ok: boolean; result: { ok: boolean; simulated?: boolean; address?: string; stdout?: string } }>(
        '/contracts/deploy',
        { contract, chain_id: chainId, constructor_args: { initialPool: defiPool }, broadcast },
      );
      if (result.result.simulated) push('warning', 'forge не найден — деплой смоделирован (установите Foundry)');
      else if (result.ok) push('success', `Деплой выполнен: ${result.result.address?.slice(0, 12) ?? ''}…`);
      else push('error', 'Деплой завершился ошибкой');
      await refetch();
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Ошибка деплоя');
    } finally {
      setBusy(false);
      setWizardOpen(false);
    }
  };

  const verify = async (row: ContractRow) => {
    try {
      const result = await api.post<{ ok: boolean; result: { simulated?: boolean } }>(`/contracts/${row.id}/verify`, {
        chain_id: row.chain_id,
      });
      push(result.ok ? 'success' : 'warning', result.result.simulated ? 'Верификация смоделирована (нет API-ключа)' : 'Верификация запущена');
      await refetch();
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Ошибка верификации');
    }
  };

  const columns: Column<ContractRow>[] = [
    {
      key: 'name',
      title: 'Контракт',
      render: (row) => (
        <div className="flex items-center gap-2">
          <Boxes size={15} className="text-[#b9a6ff]" />
          <div>
            <div className="strong text-sm">{row.name}</div>
            <div className="num text-[11px] text-[var(--text-3)]">{row.address ? `${row.address.slice(0, 12)}…${row.address.slice(-6)}` : 'не задеплоен'}</div>
          </div>
        </div>
      ),
    },
    { key: 'network', title: 'Сеть', render: (row) => <span className="text-xs">{row.network} (#{row.chain_id})</span> },
    { key: 'status', title: 'Статус', render: (row) => <StatusPill status={row.status} /> },
    {
      key: 'verified',
      title: 'Верификация',
      render: (row) =>
        row.verified ? (
          <span className="pill pill-profit"><BadgeCheck size={12} /> verified</span>
        ) : (
          <span className="pill"><ShieldQuestion size={12} /> нет</span>
        ),
    },
    {
      key: 'actions',
      title: '',
      render: (row) => (
        <div className="flex gap-2">
          <button className="btn btn-tonal !px-3 !py-1.5 text-xs" disabled={!row.address} onClick={() => void verify(row)}>
            Верифицировать
          </button>
          {row.explorer_url && (
            <a className="btn btn-outline !px-3 !py-1.5 text-xs" href={row.explorer_url} target="_blank" rel="noreferrer">
              Эксплорер
            </a>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <GlassCard
        title="Реестр контрактов"
        subtitle="FlashLoanArbitrage · Liquidator · JITVault — деплой через Foundry"
        actions={
          <button className="btn btn-filled" onClick={() => setWizardOpen(true)}>
            <Rocket size={15} /> Деплой (wizard)
          </button>
        }
        padded={false}
      >
        <DataTable columns={columns} rows={data?.items ?? []} rowKey={(row) => row.id} empty="Контракты ещё не задеплоены" />
      </GlassCard>

      <GlassCard title="Порядок деплоя" subtitle="шаги wizard-а">
        <ol className="space-y-2 text-sm text-[var(--text-2)]">
          {(data?.wizard.steps ?? ['Выбор контракта', 'Сеть и конструктор', 'Симуляция деплоя', 'Broadcast', 'Верификация']).map(
            (step, index) => (
              <li key={step} className="flex items-center gap-3">
                <span className="npill grid h-6 w-6 place-items-center rounded-full bg-[rgba(124,92,255,0.25)] text-xs">{index + 1}</span>
                {step}
              </li>
            ),
          )}
        </ol>
      </GlassCard>

      {wizardOpen && (
        <GlassCard title="Wizard деплоя" subtitle="проверьте параметры перед отправкой">
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Контракт</label>
              <select className="field" value={contract} onChange={(event) => setContract(event.target.value)}>
                {(data?.wizard.contracts ?? ['FlashLoanArbitrage', 'Liquidator', 'JITVault']).map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Сеть</label>
              <select className="field" value={chainId} onChange={(event) => setChainId(Number(event.target.value))}>
                {CHAINS.map((chain) => (
                  <option key={chain.id} value={chain.id}>{chain.name} (#{chain.id})</option>
                ))}
              </select>
            </div>
            <div className="md:col-span-2">
              <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">
                {contract === 'FlashLoanArbitrage' ? 'initialPool (WETH/USDC)' : 'аргумент конструктора'}
              </label>
              <input className="field num" value={defiPool} onChange={(event) => setDefiPool(event.target.value)} />
            </div>
            <label className="flex items-center gap-2 text-sm md:col-span-2">
              <input type="checkbox" checked={broadcast} onChange={(event) => setBroadcast(event.target.checked)} />
              Broadcast (реальная отправка транзакции; без галочки — только симуляция)
            </label>
          </div>
          <div className="mt-4 flex gap-2">
            <button className="btn btn-filled" disabled={busy} onClick={() => void deploy()}>
              {broadcast ? 'Запустить деплой' : 'Симулировать деплой'}
            </button>
            <button className="btn btn-outline" onClick={() => setWizardOpen(false)}>Отмена</button>
          </div>
        </GlassCard>
      )}
    </div>
  );
}
