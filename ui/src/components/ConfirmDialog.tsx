/** Модальное подтверждение критического действия (ввод контрольного слова/кода TOTP). */
import { AnimatePresence, motion } from 'framer-motion';
import { useState } from 'react';

interface Props {
  open: boolean;
  title: string;
  description?: string;
  confirmWord?: string;
  requireTotp?: boolean;
  confirmLabel?: string;
  danger?: boolean;
  onCancel: () => void;
  onConfirm: (payload: { word: string; totp: string }) => void | Promise<void>;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmWord,
  requireTotp,
  confirmLabel = 'Подтвердить',
  danger,
  onCancel,
  onConfirm,
}: Props) {
  const [word, setWord] = useState('');
  const [totp, setTotp] = useState('');
  const [busy, setBusy] = useState(false);
  const wordOk = !confirmWord || word.trim().toUpperCase() === confirmWord.toUpperCase();

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 grid place-items-center bg-black/55 p-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onCancel}
        >
          <motion.div
            className="glass-strong w-full max-w-md p-6"
            initial={{ opacity: 0, y: 14, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
            onClick={(event) => event.stopPropagation()}
          >
            <h3 className="text-base font-semibold">{title}</h3>
            {description && <p className="mt-2 text-sm text-[var(--text-2)]">{description}</p>}
            {confirmWord && (
              <input
                className="field mt-4"
                placeholder={`Введите ${confirmWord}`}
                value={word}
                onChange={(event) => setWord(event.target.value)}
              />
            )}
            {requireTotp && (
              <input
                className="field mt-3"
                placeholder="Код 2FA (6 цифр)"
                inputMode="numeric"
                value={totp}
                onChange={(event) => setTotp(event.target.value)}
              />
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button className="btn btn-outline" onClick={onCancel}>
                Отмена
              </button>
              <button
                className={`btn ${danger ? 'btn-danger' : 'btn-filled'}`}
                disabled={!wordOk || busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await onConfirm({ word, totp });
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {confirmLabel}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
