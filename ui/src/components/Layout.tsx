/** Каркас приложения: сайдбар + топбар + область страницы. */
import { useEffect, useState } from 'react';
import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';

export function Layout() {
  const [collapsed, setCollapsed] = useState(false);
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');

  useEffect(() => {
    const stored = localStorage.getItem('fh_theme');
    if (stored === 'light' || stored === 'dark') setTheme(stored);
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    document.documentElement.classList.toggle('light', theme === 'light');
    localStorage.setItem('fh_theme', theme);
  }, [theme]);

  return (
    <div className="flex min-h-screen">
      <Sidebar collapsed={collapsed} />
      <main className="flex min-w-0 flex-1 flex-col px-4 pb-8">
        <TopBar
          collapsed={collapsed}
          onToggleSidebar={() => setCollapsed((value) => !value)}
          theme={theme}
          onToggleTheme={() => setTheme((value) => (value === 'dark' ? 'light' : 'dark'))}
        />
        <div className="page-enter min-w-0 flex-1">
          <Outlet />
        </div>
        <footer className="mt-6 text-center text-[10px] text-[var(--text-3)]">
          FlashHunter • sandwich-атаки запрещены политикой проекта • обязательный dry-run перед live
        </footer>
      </main>
    </div>
  );
}
