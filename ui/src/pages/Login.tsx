/** Вход в систему + обязательная смена admin/admin при первом запуске. */
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import { KeyRound, Lock, ShieldCheck, User, Zap } from 'lucide-react';
import { api, setToken } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../components/Toast';

export function Login() {
  const { login, user, refresh } = useAuth();
  const { push } = useToast();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const force = params.get('force') === '1' || user?.must_change_password;

  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [totp, setTotp] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (user && !user.must_change_password) void navigate('/dashboard', { replace: true });
  }, [user, navigate]);

  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      const { mustChange } = await login(username, password, totp);
      if (mustChange) {
        push('warning', 'Пароль admin/admin требует немедленной смены');
      } else {
        push('success', 'Добро пожаловать в FlashHunter');
        void navigate('/dashboard', { replace: true });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка входа');
    } finally {
      setBusy(false);
    }
  };

  const changePassword = async () => {
    if (newPassword !== repeat) {
      setError('Пароли не совпадают');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const result = await api.post<{ token: string }>('/auth/change-password', {
        current_password: password,
        new_password: newPassword,
      });
      setToken(result.token);
      await refresh();
      push('success', 'Пароль обновлён');
      void navigate('/dashboard', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сменить пароль');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-screen place-items-center p-4">
      <motion.div
        initial={{ opacity: 0, y: 18, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.3, ease: [0.4, 0, 0.2, 1] }}
        className="glass-strong glass-edge w-full max-w-md p-8"
      >
        <div className="mb-6 flex items-center gap-3">
          <div className="grid h-12 w-12 place-items-center rounded-2xl bg-gradient-to-br from-[#7c5cff] to-[#4dc9ff] shadow-lg shadow-[#7c5cff]/40">
            <Zap size={22} className="text-white" />
          </div>
          <div>
            <h1 className="text-lg font-semibold">FlashHunter</h1>
            <p className="text-xs text-[var(--text-3)]">промышленный DeFi-комбайн</p>
          </div>
        </div>

        {!force ? (
          <div className="space-y-3">
            <label className="block text-xs uppercase tracking-wider text-[var(--text-3)]">Логин</label>
            <div className="relative">
              <User size={16} className="absolute left-3 top-3 text-[var(--text-3)]" />
              <input className="field !pl-9" value={username} onChange={(e) => setUsername(e.target.value)} />
            </div>
            <label className="block text-xs uppercase tracking-wider text-[var(--text-3)]">Пароль</label>
            <div className="relative">
              <Lock size={16} className="absolute left-3 top-3 text-[var(--text-3)]" />
              <input
                className="field !pl-9"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && void submit()}
                placeholder="admin"
              />
            </div>
            <input
              className="field"
              placeholder="Код 2FA (если включён)"
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
              inputMode="numeric"
            />
            <button className="btn btn-filled w-full" disabled={busy || !password} onClick={() => void submit()}>
              <KeyRound size={16} /> Войти
            </button>
            <p className="flex items-center gap-1.5 text-[11px] text-[var(--text-3)]">
              <ShieldCheck size={12} /> Первый вход: admin / admin — пароль будет нужно сменить
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="rounded-2xl bg-[rgba(255,181,71,0.14)] px-4 py-3 text-xs text-[#ffd79a]">
              Требуется смена пароля по умолчанию перед продолжением работы.
            </div>
            <input
              className="field"
              type="password"
              placeholder="Текущий пароль (admin)"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <input
              className="field"
              type="password"
              placeholder="Новый пароль (мин. 12 символов, 3 класса)"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
            <input
              className="field"
              type="password"
              placeholder="Повторите новый пароль"
              value={repeat}
              onChange={(e) => setRepeat(e.target.value)}
            />
            <button
              className="btn btn-filled w-full"
              disabled={busy || newPassword.length < 12}
              onClick={() => void changePassword()}
            >
              Сменить пароль и продолжить
            </button>
          </div>
        )}

        {error && <div className="mt-4 rounded-xl bg-[rgba(255,77,109,0.16)] px-4 py-2 text-xs text-[#ffb3c1]">{error}</div>}
      </motion.div>
    </div>
  );
}
