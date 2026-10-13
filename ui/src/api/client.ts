/**
 * API-клиент FlashHunter: единая точка обращения к REST и WebSocket.
 * Токен хранится в localStorage; при 401 клиент разлогинивает сессию.
 */

const TOKEN_KEY = 'fh_token';
const API_BASE = '/api/v1';

export interface ApiErrorShape {
  detail?: string | { code?: string; message?: string };
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public payload?: unknown,
  ) {
    super(message);
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (response.status === 401) {
    setToken(null);
    if (!location.pathname.startsWith('/login')) location.href = '/login';
    throw new ApiError(401, 'Сессия истекла — войдите заново');
  }
  if (!response.ok) {
    let payload: ApiErrorShape = {};
    try {
      payload = (await response.json()) as ApiErrorShape;
    } catch {
      /* пустой ответ — оставляем как есть */
    }
    const detail = payload.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : (detail as { message?: string } | undefined)?.message || `HTTP ${response.status}`;
    throw new ApiError(response.status, message, payload);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : {}) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

  /** WS-URL с токеном (браузер не умеет слать заголовки в WebSocket). */
  wsUrl(path: string): string {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const token = getToken() ?? '';
    return `${proto}://${location.host}${path}?token=${encodeURIComponent(token)}`;
  },

  /** Скачивание CSV/файлов с авторизацией (без открытия новой вкладки). */
  async download(path: string, filename: string): Promise<void> {
    const token = getToken();
    const response = await fetch(`${API_BASE}${path}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!response.ok) throw new ApiError(response.status, 'Не удалось скачать файл');
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  },
};

// --- Типы ответов API -------------------------------------------------------
export interface PnlSummary {
  period: string;
  gross_usd: number;
  net_usd: number;
  gas_usd: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  avg_profit_usd: number;
  best_usd: number;
  worst_usd: number;
  roi_pct: number;
  sharpe: number;
  max_drawdown_usd: number;
}

export interface Overview {
  pnl: Record<string, PnlSummary>;
  equity_curve: { ts: string; pnl_usd: number; cumulative_usd: number }[];
  wallets: {
    id: number;
    name: string;
    address: string;
    is_hot: boolean;
    balance_usd: number;
    native_balance: number;
    chains: number[];
  }[];
  services: {
    strategy_service: Record<string, number>;
    market: Record<string, number | null>;
    strategies_enabled: string[];
    relays: RelayStat[];
    health: { service: string; status: string; latency_ms: number; last_seen: string }[];
  };
  risk: RiskState;
  kill_switch: boolean;
  kill_reason: string;
  dry_run: boolean;
  trades_today: number;
  user: string;
}

export interface RelayStat {
  relay: string;
  enabled: boolean;
  sent: number;
  accepted: number;
  failed: number;
  success_rate_pct: number;
  avg_latency_ms: number;
}

export interface RiskState {
  kill_switch: boolean;
  kill_reason: string;
  dry_run: boolean;
  daily_pnl_usd: number;
  hourly_pnl_usd: number;
  daily_limit_usd: number;
  hourly_limit_usd: number;
  revert_rate_pct: number;
  revert_limit_pct: number;
  latency_p99_ms: number;
  latency_limit_ms: number;
  gas_ceiling_gwei: number;
  slippage_ceiling_bps: number;
  min_profit_usd: number;
  max_size_usd: number;
  exposure: { chains: Record<string, number>; tokens: Record<string, number> };
}

export interface OpportunityRow {
  uid: string;
  strategy: string;
  chain_id: number;
  token_in: string;
  token_out: string;
  dex_buy: string;
  dex_sell: string;
  size_usd: number;
  gross_profit_usd: number;
  gas_usd: number;
  flash_fee_usd: number;
  net_profit_usd: number;
  roi_pct: number;
  confidence: number;
  block_number: number;
  pools: string[];
  kind: string;
  meta: Record<string, string | number>;
}

export interface ExecutionEvent {
  uid: string;
  accepted: boolean;
  status: string;
  net_profit_usd: number;
  bundle_hash: string;
  relay: string;
  reason: string;
  latency_ms: number;
  manual?: boolean;
}
