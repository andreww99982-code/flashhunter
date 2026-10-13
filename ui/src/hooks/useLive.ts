/**
 * Живое соединение с оркестратором: WS-события (возможности, сделки, логи) + снапшот состояния.
 * При обрыве — авто-переподключение с экспоненциальной паузой.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';

export interface LiveEvent {
  type: string;
  data: Record<string, unknown>;
  ts?: number;
}

export interface LiveState {
  connected: boolean;
  lastEvent: LiveEvent | null;
  events: LiveEvent[];
  state: Record<string, unknown> | null;
}

const MAX_EVENTS = 200;

export function useLive(enabled = true): LiveState {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [state, setState] = useState<Record<string, unknown> | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);

  useEffect(() => {
    if (!enabled) return undefined;
    let closed = false;
    let timer: number | undefined;

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(api.wsUrl('/ws'));
      socketRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        retryRef.current = 0;
      };
      ws.onmessage = (message) => {
        try {
          const parsed = JSON.parse(message.data as string) as LiveEvent;
          if (parsed.type === 'state') {
            setState(parsed.data);
            return;
          }
          setEvents((prev) => [parsed, ...prev].slice(0, MAX_EVENTS));
        } catch {
          /* игнорируем некорректные кадры */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        retryRef.current = Math.min(retryRef.current + 1, 6);
        timer = window.setTimeout(connect, 500 * 2 ** retryRef.current);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socketRef.current?.close();
    };
  }, [enabled]);

  const lastEvent = events.length > 0 ? events[0] : null;
  return useMemo(() => ({ connected, lastEvent, events, state }), [connected, lastEvent, events, state]);
}
