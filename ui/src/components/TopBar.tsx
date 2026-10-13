/** Верхняя панель: блок, base fee, режим dry-run, kill-switch, тема, пользователь. */
import { useState } from 'react';
import { AlertOctagon, Flame, Moon, PanelLeftClose, PanelLeftOpen, Power, Sun } from 'lucide-react';
import { api } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import { useToast } from './Toast';
import { useLive } from '../hooks/useLive';

interface Props {
  onToggleSidebar: () => void;
  collapsed: boolean;
  theme: 'dark' | 'light';
  onToggleTheme: () => void;
}

export function TopBar({ onToggleSidebar, collapsed, theme, onToggleTheme }: Props) {
  const { user, logout } = useAuth();
  const { push } = useToast();
  const live = useLive(true);
  const [busy, setBusy] = useState(false);

  const state = live.state as
    | {
        kill_switch?: boolean;
        dry_run?: boolean;
        market?: { block_number?: number; base_fee_gwei?: number; native_price_usd?: number };
      }
    | null;

  const toggleKillSwitch = async (engage: boolean) => {
    setBusy(true);
    try {
      await api.post('/settings/kill-switch', { engage, reason: engage ? 'manual (topbar)' : 'manual release' });
      push(engage ? 'warning' : 'success', engage ? 'KILL-SWITCH взведён: исполнение остановлено' : 'Kill-switch снят, работа возобновлена');
    } catch (error) {
      push('error', error instanceof Error ? error.message : 'Не удалось переключить kill-switch');
    } finally {
      setBusy(false);
    }
  };

  return (
    <header className="glass glass-edge sticky top-0 z-40 mb-4 flex flex-wrap items-center gap-3 px-4 py-3">
      <button className="btn btn-outline !px-2" onClick={onToggleSidebar} title="Свернуть меню">
        {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
      </button>

      {state?.dry_run !== false && (
        <span className="pill pill-warn" title="Сделки не отправляются в сеть">
          <Flame size={13} /> DRY-RUN
        </span>
      )}
      {live.connected ? (
        <span className="pill pill-profit">
          <span className="live-dot" /> LIVE
        </span>
      ) : (
        <span className="pill pill-loss">нет WS-соединения</span>
      )}

      <div className="hidden items-center gap-4 text-xs text-[var(--text-2)] md:flex">
        <span className="num">
          блок <b className="text-[var(--text-1)]">{state?.market?.block_number ?? '—'}</b>
        </span>
        <span className="num">
          base fee <b className="text-[var(--text-1)]">{state?.market?.base_fee_gwei?.toFixed(2) ?? '—'}</b> gwei
        </span>
        <span className="num">
          ETH <b className="text-[var(--text-1)]">${state?.market?.native_price_usd?.toFixed(0) ?? '—'}</b>
        </span>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <button
          className={`btn ${state?.kill_switch ? 'btn-success' : 'btn-danger'}`}
          disabled={busy}
          onClick={() => void toggleKillSwitch(!state?.kill_switch)}
          title="Экстренная остановка исполнения"
        >
          {state?.kill_switch ? <Power size={16} /> : <AlertOctagon size={16} />}
          {state?.kill_switch ? 'Снять KILL-SWITCH' : 'KILL-SWITCH'}
        </button>
        <button className="btn btn-outline !px-2" onClick={onToggleTheme} title="Сменить тему">
          {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
        </button>
        <div className="hidden items-center gap-2 pl-2 text-xs text-[var(--text-2)] sm:flex">
          <span>{user?.username}</span>
          <button className="btn btn-outline" onClick={() => void logout()}>
            Выйти
          </button>
        </div>
      </div>
    </header>
  );
}
