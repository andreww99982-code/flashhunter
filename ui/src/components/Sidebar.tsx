/** Боковая навигация (M3 navigation drawer) с иконками Lucide. */
import { NavLink } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  Activity,
  BarChart3,
  Boxes,
  FileClock,
  Gauge,
  LayoutDashboard,
  ScrollText,
  Settings,
  ShieldCheck,
  Sparkles,
  Target,
  TestTube2,
  Wallet,
  Zap,
} from 'lucide-react';

const links = [
  { to: '/dashboard', label: 'Дашборд', icon: LayoutDashboard },
  { to: '/opportunities', label: 'Возможности', icon: Target },
  { to: '/strategies', label: 'Стратегии', icon: Sparkles },
  { to: '/contracts', label: 'Контракты', icon: Boxes },
  { to: '/wallets', label: 'Кошельки', icon: Wallet },
  { to: '/history', label: 'История', icon: FileClock },
  { to: '/analytics', label: 'Аналитика', icon: BarChart3 },
  { to: '/latency', label: 'Латентность', icon: Gauge },
  { to: '/backtest', label: 'Бэктест', icon: TestTube2 },
  { to: '/settings', label: 'Настройки', icon: Settings },
  { to: '/logs', label: 'Логи', icon: ScrollText },
];

export function Sidebar({ collapsed }: { collapsed: boolean }) {
  return (
    <motion.aside
      animate={{ width: collapsed ? 76 : 248 }}
      transition={{ duration: 0.24, ease: [0.4, 0, 0.2, 1] }}
      className="glass sticky top-0 z-30 flex h-screen shrink-0 flex-col gap-1 overflow-hidden p-3"
      style={{ borderRadius: 0, borderLeft: 'none', borderTop: 'none', borderBottom: 'none' }}
    >
      <div className="mb-4 flex items-center gap-3 px-2 py-2">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-[#7c5cff] to-[#4dc9ff] shadow-lg shadow-[#7c5cff]/40">
          <Zap size={20} className="text-white" />
        </div>
        {!collapsed && (
          <div className="leading-tight">
            <div className="text-sm font-semibold tracking-wide">FlashHunter</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-[var(--text-3)]">DeFi comb</div>
          </div>
        )}
      </div>

      <nav className="flex flex-1 flex-col gap-1">
        {links.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            title={label}
            className={({ isActive }) =>
              [
                'group flex items-center gap-3 rounded-2xl px-3 py-2.5 text-sm transition-all duration-200',
                isActive
                  ? 'bg-[rgba(124,92,255,0.22)] text-[var(--text-1)] shadow-[inset_0_0_0_1px_rgba(124,92,255,0.35)]'
                  : 'text-[var(--text-2)] hover:bg-[var(--state-hover)]',
              ].join(' ')
            }
          >
            <Icon size={18} className="shrink-0" />
            {!collapsed && <span className="truncate">{label}</span>}
          </NavLink>
        ))}
      </nav>

      <div className="rounded-2xl bg-white/5 px-3 py-2 text-[10px] leading-relaxed text-[var(--text-3)]">
        {collapsed ? <Activity size={14} /> : <><ShieldCheck size={12} className="mr-1 inline" /> sandwich запрещён политикой</>}
      </div>
    </motion.aside>
  );
}
