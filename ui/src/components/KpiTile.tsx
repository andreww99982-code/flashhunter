/** KPI-плитка: значение, подпись, дельта и иконка. */
import { motion } from 'framer-motion';
import type { ReactNode } from 'react';

interface Props {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  tone?: 'neutral' | 'profit' | 'loss' | 'primary' | 'warn';
  delay?: number;
}

const toneClass: Record<NonNullable<Props['tone']>, string> = {
  neutral: 'text-[var(--text-1)]',
  profit: 'profit',
  loss: 'loss',
  primary: 'text-[#b9a6ff]',
  warn: 'text-[var(--accent-warn)]',
};

export function KpiTile({ label, value, hint, icon, tone = 'neutral', delay = 0 }: Props) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: [0.4, 0, 0.2, 1], delay }}
      className="glass glass-edge card-hover flex flex-col gap-2 p-5"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-[var(--text-3)]">{label}</span>
        {icon && <span className="text-[var(--text-3)]">{icon}</span>}
      </div>
      <div className={`num text-2xl font-semibold ${toneClass[tone]}`}>{value}</div>
      {hint && <div className="text-xs text-[var(--text-3)]">{hint}</div>}
    </motion.div>
  );
}
