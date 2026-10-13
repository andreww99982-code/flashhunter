/** Контекст авторизации: токен, профиль пользователя, выход. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, getToken, setToken } from '../api/client';

interface Me {
  username: string;
  role: string;
  must_change_password: boolean;
  totp_enabled: boolean;
  last_login: string | null;
}

interface AuthContextValue {
  user: Me | null;
  loading: boolean;
  login: (username: string, password: string, totp?: string) => Promise<{ mustChange: boolean }>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      setUser(await api.get<Me>('/auth/me'));
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string, totp?: string) => {
    const result = await api.post<{ token: string; must_change_password: boolean }>('/auth/login', {
      username,
      password,
      totp: totp || undefined,
    });
    setToken(result.token);
    await refresh();
    return { mustChange: result.must_change_password };
  }, [refresh]);

  const logout = useCallback(async () => {
    try {
      await api.post('/auth/logout');
    } catch {
      /* даже если сервер недоступен — чистим токен локально */
    }
    setToken(null);
    setUser(null);
  }, []);

  const value = useMemo(() => ({ user, loading, login, logout, refresh }), [user, loading, login, logout, refresh]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}
