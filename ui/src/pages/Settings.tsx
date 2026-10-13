/** Настройки: риск-лимиты, режим live/dry-run, kill-switch, API-ключи, TOTP, сессии. */
import { useEffect, useState } from 'react';
import { KeyRound, Plus, ShieldCheck, ShieldOff, Trash2, Zap } from 'lucide-react';
import { api } from '../api/client';
import { GlassCard } from '../components/GlassCard';
import { DataTable, type Column } from '../components/DataTable';
import { StatusPill } from '../components/StatusPill';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { useApi } from '../hooks/useApi';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../components/Toast';
import type { RiskState } from '../api/client';

interface SettingsResponse {
  risk: RiskState;
  limits: { id: number; scope: string; key: string; value: Record<string, unknown>; updated_at: string }[];
  settings: Record<string, unknown>;
  api_keys: { id: number; provider: string; masked: string; created_at: string }[];
  strategies_available: string[];
}

interface RuntimeConfigField {
  name: string;
  label: string;
  category: string;
  kind: 'boolean' | 'number' | 'list' | 'select' | 'secret' | 'text';
  value: unknown;
  configured: boolean;
  masked: string | null;
  choices: (string | number)[];
  description: string;
}

const EDITABLE: { key: string; label: string; hint: string }[] = [
  { key: 'daily_loss_limit_usd', label: 'Дневной лимит убытка, $', hint: 'при превышении — автоматический kill-switch' },
  { key: 'hourly_loss_limit_usd', label: 'Часовой лимит убытка, $', hint: 'защита от серии ревертов' },
  { key: 'gas_ceiling_gwei', label: 'Потолок газа, gwei', hint: 'сделки дороже — отклоняются' },
  { key: 'max_slippage_bps', label: 'Потолок слиппеджа, bps', hint: 'ограничение проскальзывания' },
  { key: 'latency_p99_limit_ms', label: 'Лимит p99 латентности, мс', hint: 'превышение — пауза стратегий' },
  { key: 'connection_loss_sec', label: 'Допустимая потеря связи, с', hint: 'при дольшем отсутствии блоков — kill-switch' },
  { key: 'revert_rate_limit_pct', label: 'Лимит доли ревертов, %', hint: 'превышение — пауза' },
];

export function SettingsPage() {
  const { data, refetch } = useApi<SettingsResponse>('/settings', 12000);
  const runtimeConfig = useApi<{
    fields: RuntimeConfigField[];
    locked_fields: { name: string; label: string; category: string; reason: string }[];
    overrides: string[];
  }>('/settings/config', 30000);
  const { user, refresh } = useAuth();
  const { push } = useToast();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [liveOpen, setLiveOpen] = useState(false);
  const [totp, setTotp] = useState<{ secret: string; uri: string; qr: string } | null>(null);
  const [totpCode, setTotpCode] = useState('');
  const [newKey, setNewKey] = useState({ provider: 'etherscan', key: '' });
  const [configDrafts, setConfigDrafts] = useState<Record<string, unknown>>({});

  const saveConfig = async (field: RuntimeConfigField) => {
    const value = configDrafts[field.name] ?? field.value;
    try {
      const result = await api.put<{ restart_required: boolean }>('/settings/config', { values: { [field.name]: value } });
      setConfigDrafts((previous) => {
        const next = { ...previous };
        delete next[field.name];
        return next;
      });
      push('success', result.restart_required ? 'Настройка сохранена. Перезапустите сервис для применения.' : 'Настройка сохранена');
      await runtimeConfig.refetch();
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Не удалось сохранить настройку');
    }
  };

  useEffect(() => {
    if (!data) return;
    const next: Record<string, string> = {};
    for (const limit of data.limits) {
      const numeric = limit.value.limit ?? limit.value.max_exposure_usd;
      next[`${limit.scope}:${limit.key}`] = numeric === undefined ? '' : String(numeric);
    }
    setDrafts(next);
  }, [data]);

  const saveLimit = async (key: string) => {
    const raw = drafts[`global:${key}`];
    if (raw === undefined || raw === '') return;
    try {
      await api.put('/settings/limits', { scope: 'global', key, value: { limit: Number(raw) } });
      push('success', `Лимит ${key} = ${raw} применён`);
      await refetch();
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Ошибка сохранения лимита');
    }
  };

  const settingColumns: Column<{ key: string; value: unknown }>[] = [
    { key: 'key', title: 'Параметр', render: (row) => <span className="num text-sm">{row.key}</span> },
    {
      key: 'value',
      title: 'Значение',
      render: (row) => <span className="num text-xs text-[var(--text-2)]">{JSON.stringify(row.value).slice(0, 120)}</span>,
    },
  ];

  const keyColumns: Column<SettingsResponse['api_keys'][number]>[] = [
    { key: 'provider', title: 'Провайдер', render: (row) => <span className="pill pill-info">{row.provider}</span> },
    { key: 'masked', title: 'Ключ', render: (row) => <span className="num text-xs">{row.masked}</span> },
    { key: 'created', title: 'Добавлен', render: (row) => <span className="num text-xs">{row.created_at.replace('T', ' ').slice(0, 19)}</span> },
    {
      key: 'actions',
      title: '',
      render: (row) => (
        <button
          className="btn btn-outline !px-3 !py-1.5 text-xs"
          onClick={async () => {
            await api.del(`/settings/api-keys/${row.id}`);
            push('success', 'Ключ удалён');
            await refetch();
          }}
        >
          <Trash2 size={13} /> Удалить
        </button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard
          title="Риск-лимиты"
          subtitle="применяются к живому риск-движку без перезапуска"
          actions={<StatusPill status={data?.risk.kill_switch ? 'failed' : 'ok'} label={data?.risk.kill_switch ? 'KILL-SWITCH' : 'работа разрешена'} />}
        >
          <div className="space-y-3">
            {EDITABLE.map((item) => {
              // соответствие ключа лимита полю риск-состояния для отображения текущего значения
              const stateKey: Record<string, string> = {
                daily_loss_limit_usd: 'daily_limit_usd',
                hourly_loss_limit_usd: 'hourly_limit_usd',
                gas_ceiling_gwei: 'gas_ceiling_gwei',
                max_slippage_bps: 'slippage_ceiling_bps',
                latency_p99_limit_ms: 'latency_limit_ms',
                connection_loss_sec: 'connection_loss_sec',
                revert_rate_limit_pct: 'revert_limit_pct',
              };
              const current = (data?.risk as unknown as Record<string, number> | undefined)?.[stateKey[item.key]];
              return (
                <div key={item.key}>
                  <label className="mb-1 flex items-center justify-between text-[11px] uppercase tracking-wider text-[var(--text-3)]">
                    <span>{item.label}</span>
                    <span className="num text-[var(--text-2)]">{current !== undefined ? String(current) : ''}</span>
                  </label>
                  <div className="flex gap-2">
                    <input
                      className="field num"
                      type="number"
                      step="0.01"
                      value={drafts[`global:${item.key}`] ?? ''}
                      onChange={(event) => setDrafts((prev) => ({ ...prev, [`global:${item.key}`]: event.target.value }))}
                    />
                    <button className="btn btn-tonal" onClick={() => void saveLimit(item.key)}>OK</button>
                  </div>
                  <p className="mt-1 text-[10px] text-[var(--text-3)]">{item.hint}</p>
                </div>
              );
            })}
          </div>
        </GlassCard>

        <div className="space-y-4">
          <GlassCard title="Режим исполнения" subtitle="live требует dry-run ≥ 24 ч (ТЗ §10)">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm">
                  Текущий режим:{' '}
                  <b className={data?.risk.dry_run ? 'text-[var(--accent-warn)]' : 'profit'}>
                    {data?.risk.dry_run ? 'DRY-RUN' : 'LIVE'}
                  </b>
                </div>
                <p className="mt-1 text-xs text-[var(--text-3)]">
                  {data?.risk.dry_run
                    ? 'Сделки симулируются и логируются, но не отправляются в сеть.'
                    : 'Внимание: реальные средства. Каждая сделка уходит в релей.'}
                </p>
              </div>
              {data?.risk.dry_run ? (
                <button className="btn btn-danger" onClick={() => setLiveOpen(true)}>
                  <Zap size={15} /> Перейти в LIVE
                </button>
              ) : (
                <button
                  className="btn btn-outline"
                  onClick={async () => {
                    await api.post('/settings/mode', { live: false });
                    push('success', 'Возврат в dry-run');
                    await refetch();
                  }}
                >
                  Вернуться в dry-run
                </button>
              )}
            </div>
            <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
              <div className="glass px-3 py-2">
                <div className="text-[var(--text-3)]">PnL 24 ч</div>
                <div className={`num font-semibold ${(data?.risk.daily_pnl_usd ?? 0) >= 0 ? 'profit' : 'loss'}`}>
                  ${(data?.risk.daily_pnl_usd ?? 0).toFixed(2)} / лимит ${data?.risk.daily_limit_usd}
                </div>
              </div>
              <div className="glass px-3 py-2">
                <div className="text-[var(--text-3)]">Реверты</div>
                <div className="num font-semibold">
                  {(data?.risk.revert_rate_pct ?? 0).toFixed(1)}% / лимит {data?.risk.revert_limit_pct}%
                </div>
              </div>
              <div className="glass px-3 py-2">
                <div className="text-[var(--text-3)]">Экспозиция цепей</div>
                <div className="num">
                  {Object.entries(data?.risk.exposure.chains ?? {}).map(([chain, value]) => `#${chain}: $${Number(value).toFixed(0)}`).join(' · ') || '—'}
                </div>
              </div>
              <div className="glass px-3 py-2">
                <div className="text-[var(--text-3)]">Max размер сделки</div>
                <div className="num">${(data?.risk.max_size_usd ?? 0).toLocaleString('ru-RU')}</div>
              </div>
            </div>
          </GlassCard>

          <GlassCard title="Двухфакторная аутентификация" subtitle="TOTP (Google Authenticator, Aegis)">
            {user?.totp_enabled ? (
              <div className="flex items-center gap-3 text-sm">
                <ShieldCheck size={18} className="profit" />
                2FA включена. Код требуется при входе и для критических действий.
              </div>
            ) : (
              <div className="space-y-3">
                <div className="flex items-center gap-3 text-sm text-[var(--text-2)]">
                  <ShieldOff size={18} className="text-[var(--accent-warn)]" />
                  2FA выключена — рекомендуется включить.
                </div>
                {!totp ? (
                  <button
                    className="btn btn-tonal"
                    onClick={async () => {
                      const result = await api.post<{ secret: string; uri: string; qr: string }>('/auth/totp/setup');
                      setTotp(result);
                    }}
                  >
                    <KeyRound size={14} /> Начать настройку
                  </button>
                ) : (
                  <div className="space-y-2">
                    <img src={totp.qr} alt="TOTP QR" className="mx-auto h-40 w-40 rounded-2xl bg-white p-2" />
                    <p className="num break-all text-center text-[11px] text-[var(--text-3)]">секрет: {totp.secret}</p>
                    <div className="flex gap-2">
                      <input className="field num" placeholder="6-значный код" value={totpCode} onChange={(e) => setTotpCode(e.target.value)} />
                      <button
                        className="btn btn-filled"
                        onClick={async () => {
                          try {
                            await api.post('/auth/totp/enable', { code: totpCode, secret: totp.secret });
                            push('success', '2FA включена');
                            setTotp(null);
                            await refresh();
                          } catch (error) {
                            push('error', error instanceof Error ? error.message : 'Неверный код');
                          }
                        }}
                      >
                        Включить
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
            <button
              className="btn btn-outline mt-3"
              onClick={async () => {
                const result = await api.del<{ revoked: number }>('/auth/sessions');
                push('success', `Сессии отозваны: ${result.revoked ?? 0}`);
              }}
            >
              Отозвать все сессии
            </button>
          </GlassCard>
        </div>
      </div>

      <GlassCard
        title="Полная конфигурация"
        subtitle="Параметры приложения, торговли и хранения данных настраиваются здесь; .env не требуется."
      >
        <div className="mb-3 rounded-xl border border-[var(--accent-warn)]/30 bg-[var(--accent-warn)]/5 p-3 text-xs text-[var(--text-2)]">
          Настройки сохраняются между перезапусками и загружаются до подключения к базе. Первый запуск использует
          локальную SQLite и реальные данные сети; сделки остаются в DRY-RUN до отдельного подтверждения.
          После смены хранилища данные автоматически не переносятся. Секреты не показываются повторно.
          Приватные ключи торговых кошельков настраиваются на странице «Кошельки».
        </div>
        <div className="space-y-3">
          {Object.entries(
            (runtimeConfig.data?.fields ?? []).reduce<Record<string, RuntimeConfigField[]>>((groups, field) => {
              (groups[field.category] ??= []).push(field);
              return groups;
            }, {}),
          ).map(([category, fields]) => (
            <details key={category} className="glass rounded-2xl p-4" open={category === 'Сети и RPC'}>
              <summary className="cursor-pointer text-sm font-semibold">{category} <span className="text-xs text-[var(--text-3)]">({fields.length})</span></summary>
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                {fields.map((field) => {
                  const edited = Object.prototype.hasOwnProperty.call(configDrafts, field.name);
                  const value = edited ? configDrafts[field.name] : field.value;
                  return (
                    <div key={field.name} className="min-w-0">
                      <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">
                        {field.label}
                      </label>
                      {field.kind === 'boolean' ? (
                        <label className="flex min-h-11 items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            checked={Boolean(value)}
                            onChange={(event) => setConfigDrafts((previous) => ({ ...previous, [field.name]: event.target.checked }))}
                          />
                          {Boolean(value) ? 'Включено' : 'Выключено'}
                        </label>
                      ) : field.kind === 'select' ? (
                        <select
                          className="field"
                          value={String(value ?? '')}
                          onChange={(event) => setConfigDrafts((previous) => ({ ...previous, [field.name]: event.target.value }))}
                        >
                          {field.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                        </select>
                      ) : field.kind === 'list' ? (
                        <textarea
                          className="field num min-h-20"
                          value={Array.isArray(value) ? value.join(', ') : String(value ?? '')}
                          placeholder="Значения через запятую"
                          onChange={(event) => {
                            const values = event.target.value.split(',').map((part) => part.trim()).filter(Boolean);
                            setConfigDrafts((previous) => ({
                              ...previous,
                              [field.name]: field.name === 'strat_arb_chains' ? values.map(Number) : values,
                            }));
                          }}
                        />
                      ) : (
                        <input
                          className="field num"
                          type={field.kind === 'secret' ? 'password' : field.kind === 'number' ? 'number' : 'text'}
                          value={field.kind === 'secret' ? String(configDrafts[field.name] ?? '') : String(value ?? '')}
                          placeholder={field.kind === 'secret' ? (field.configured ? `Настроено: ${field.masked}; введите новое значение для замены` : 'Не задано') : ''}
                          onChange={(event) => setConfigDrafts((previous) => ({
                            ...previous,
                            [field.name]: field.kind === 'number' ? Number(event.target.value) : event.target.value,
                          }))}
                        />
                      )}
                      <div className="mt-2 flex items-center justify-between gap-2">
                        <span className="break-all text-[10px] text-[var(--text-3)]">{field.description || field.name}</span>
                        <button
                          className="btn btn-tonal shrink-0 !px-3 !py-1.5 text-xs"
                          disabled={!edited || (field.kind === 'secret' && !String(configDrafts[field.name] ?? ''))}
                          onClick={() => void saveConfig(field)}
                        >
                          Сохранить
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </details>
          ))}
        </div>
      </GlassCard>

      {(runtimeConfig.data?.locked_fields.length ?? 0) > 0 && (
        <GlassCard title="Системные исключения" subtitle="Не относятся к прикладной конфигурации">
          <p className="mb-3 text-xs text-[var(--text-3)]">
            Пароль меняется в профиле. Имитация рынка отключена. Адрес и порт слушающего API фиксируются
            точкой запуска, чтобы панель не могла отключить сама себя.
          </p>
          <div className="flex flex-wrap gap-2">
            {runtimeConfig.data?.locked_fields.map((field) => (
              <span key={field.name} className="pill" title={`${field.category}: ${field.reason}`}>{field.label}</span>
            ))}
          </div>
        </GlassCard>
      )}

      <GlassCard
        title="API-ключи внешних сервисов"
        subtitle="Etherscan, Tenderly, RPC-провайдеры — шифруются в БД"
        padded={false}
      >
        <div className="flex flex-wrap items-end gap-3 px-5 py-4">
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Провайдер</label>
            <select className="field !w-44" value={newKey.provider} onChange={(e) => setNewKey({ ...newKey, provider: e.target.value })}>
              {['etherscan', 'tenderly', 'alchemy', 'infura', 'quicknode', 'bloxroute', 'helius'].map((provider) => (
                <option key={provider} value={provider}>{provider}</option>
              ))}
            </select>
          </div>
          <div className="min-w-[240px] flex-1">
            <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--text-3)]">Ключ</label>
            <input className="field num" type="password" value={newKey.key} onChange={(e) => setNewKey({ ...newKey, key: e.target.value })} />
          </div>
          <button
            className="btn btn-filled"
            disabled={!newKey.key}
            onClick={async () => {
              try {
                await api.post('/settings/api-keys', { provider: newKey.provider, api_key: newKey.key });
                push('success', 'Ключ сохранён (хранится в зашифрованном виде)');
                setNewKey({ provider: newKey.provider, key: '' });
                await refetch();
              } catch (error) {
                push('error', error instanceof Error ? error.message : 'Ошибка сохранения ключа');
              }
            }}
          >
            <Plus size={15} /> Добавить
          </button>
        </div>
        <DataTable columns={keyColumns} rows={data?.api_keys ?? []} rowKey={(row) => row.id} empty="Внешние ключи не добавлены" />
      </GlassCard>

      <GlassCard title="Прочие настройки панели" padded={false}>
        <DataTable
          columns={settingColumns}
          rows={Object.entries(data?.settings ?? {}).map(([key, value]) => ({ key, value }))}
          rowKey={(row) => row.key}
          empty="Нет сохранённых настроек"
        />
      </GlassCard>

      <ConfirmDialog
        open={liveOpen}
        title="Переход в LIVE-режим"
        description="Требования: dry-run не менее 24 часов, отсутствие активного kill-switch, подтверждение словом LIVE. Сделки начнут отправляться в релеи с реальными средствами."
        confirmWord="LIVE"
        confirmLabel="Включить LIVE"
        danger
        onCancel={() => setLiveOpen(false)}
        onConfirm={async () => {
          try {
            await api.post('/settings/mode', { live: true, confirmation: 'LIVE' });
            push('warning', 'LIVE-режим включён — следите за рисками');
          } catch (error) {
            push('error', error instanceof Error ? error.message : 'Переход отклонён');
          } finally {
            setLiveOpen(false);
            await refetch();
          }
        }}
      />
    </div>
  );
}
