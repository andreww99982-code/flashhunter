/** Всплывающие уведомления (M3 snackbar) с контекстом использования. */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react';

type ToastKind = 'success' | 'error' | 'info' | 'warning';
interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

const icons: Record<ToastKind, ReactNode> = {
  success: <CheckCircle2 size={18} className="text-[var(--accent-profit)]" />,
  error: <XCircle size={18} className="text-[var(--accent-loss)]" />,
  info: <Info size={18} className="text-[var(--accent-info)]" />,
  warning: <AlertTriangle size={18} className="text-[var(--accent-warn)]" />,
};

const ToastContext = createContext<{ push: (kind: ToastKind, message: string) => void } | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const push = useCallback((kind: ToastKind, message: string) => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev, { id, kind, message }].slice(-4));
    window.setTimeout(() => setItems((prev) => prev.filter((item) => item.id !== id)), 5200);
  }, []);

  const value = useMemo(() => ({ push }), [push]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-6 right-6 z-[60] flex w-[min(92vw,380px)] flex-col gap-2">
        <AnimatePresence>
          {items.map((item) => (
            <motion.div
              key={item.id}
              initial={{ opacity: 0, y: 16, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98 }}
              transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
              className="glass-strong pointer-events-auto flex items-start gap-3 px-4 py-3 text-sm"
            >
              {icons[item.kind]}
              <span className="text-[var(--text-1)]">{item.message}</span>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) throw new Error('useToast must be used inside ToastProvider');
  return context;
}
