/** Корневой компонент: маршрутизация и защита маршрутов авторизацией. */
import { Navigate, Outlet, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { useAuth } from './hooks/useAuth';
import { Login } from './pages/Login';
import { Dashboard } from './pages/Dashboard';
import { Opportunities } from './pages/Opportunities';
import { Strategies } from './pages/Strategies';
import { Contracts } from './pages/Contracts';
import { Wallets } from './pages/Wallets';
import { History } from './pages/History';
import { Analytics } from './pages/Analytics';
import { Latency } from './pages/Latency';
import { SettingsPage } from './pages/Settings';
import { Logs } from './pages/Logs';
import { Backtest } from './pages/Backtest';

function Protected() {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center">
        <div className="glass px-6 py-5 text-sm text-[var(--text-2)]">Проверка сессии…</div>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  return <Outlet />;
}

function ChangePasswordGate() {
  const { user } = useAuth();
  if (user?.must_change_password) return <Navigate to="/login?force=1" replace />;
  return <Outlet />;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Protected />}>
        <Route element={<ChangePasswordGate />}>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/opportunities" element={<Opportunities />} />
            <Route path="/strategies" element={<Strategies />} />
            <Route path="/contracts" element={<Contracts />} />
            <Route path="/wallets" element={<Wallets />} />
            <Route path="/history" element={<History />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/latency" element={<Latency />} />
            <Route path="/backtest" element={<Backtest />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/logs" element={<Logs />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
