/** Хук опроса API с интервалом и ручным refetch (используется всеми страницами). */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api/client';

export interface UseApiResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
}

export function useApi<T>(path: string, intervalMs = 0, enabled = true): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  const refetch = useCallback(async () => {
    if (!enabled) return;
    try {
      const result = await api.get<T>(path);
      if (mounted.current) {
        setData(result);
        setError(null);
      }
    } catch (err) {
      if (mounted.current) {
        setError(err instanceof ApiError ? err.message : 'Ошибка сети');
      }
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [path, enabled]);

  useEffect(() => {
    mounted.current = true;
    void refetch();
    return () => {
      mounted.current = false;
    };
  }, [refetch]);

  useEffect(() => {
    if (!intervalMs || !enabled) return undefined;
    const timer = window.setInterval(() => void refetch(), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, refetch, enabled]);

  return { data, loading, error, refetch };
}
