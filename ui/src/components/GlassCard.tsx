/** Универсальная стеклянная карточка (Liquid Glass). */
import { motion } from 'framer-motion';
import type { ReactNode } from 'react';

interface Props {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
  delay?: number;
}

export function GlassCard({ title, subtitle, actions, children, className = '', padded = true, delay = 0 }: Props) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.26, ease: [0.4, 0, 0.2, 1], delay }}
      className={`glass glass-edge card-hover ${className}`}
    >
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-4">
          <div>
            {title && <h3 className="text-sm font-semibold tracking-wide text-[var(--text-1)]">{title}</h3>}
            {subtitle && <p className="mt-0.5 text-xs text-[var(--text-3)]">{subtitle}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={padded ? 'p-5' : ''}>{children}</div>
    </motion.section>
  );
}
