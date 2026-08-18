// 后端 JSON API 封装（API Key 全面鉴权）

const KEY_STORAGE = 'mihomo_ui_key';

export function getStoredKey(): string {
  try { return localStorage.getItem(KEY_STORAGE) || ''; } catch { return ''; }
}
export function setStoredKey(key: string) {
  try { localStorage.setItem(KEY_STORAGE, key); } catch { /* ignore */ }
}
export function clearStoredKey() {
  try { localStorage.removeItem(KEY_STORAGE); } catch { /* ignore */ }
}

export interface ConnInfo {
  enabled: boolean;
  host: string;
  port: string;
  username: string;
  masked_url: string;
  url: string;
}

export interface Session {
  task_id: string;
  proxy: string;
  listener: string;
  listener_port: number;
  acquired_at: string;
  expires_at: string | null;
  status: string;
  scenario: string;
}

export interface Status {
  alive: boolean;
  ip: string;
  country: string;
  mode?: string;
  sticky_enabled?: boolean;
}

export interface SpeedResult {
  speed: number;
  latency: number;
  bar_width: number;
}

export interface Bootstrap {
  ok: boolean;
  error?: string;
  message?: string;
  settings: any;
  api_key: string;
  status: Status;
  sticky: { enabled: boolean; test_url: string; test_enabled: boolean; timeout: number };
  pool: { total: number; available: number; in_use: number };
  sessions: Session[];
  conn_urls: Record<'socks' | 'http', ConnInfo>;
  entry_mode_label: string;
  conn_ready: boolean;
  config_text: string;
  server: string;
}

async function jsend(url: string, body?: any): Promise<any> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const key = getStoredKey();
  if (key) headers['X-API-Key'] = key;
  const opt: RequestInit = {
    method: body !== undefined ? 'POST' : 'GET',
    credentials: 'same-origin',
    headers,
  };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  let data: any = null;
  try { data = await r.json(); } catch { /* ignore */ }
  if (!r.ok && !data) throw new Error(`HTTP ${r.status}`);
  return data;
}

export const api = {
  login: (key: string) => jsend('/api/ui/login', { key }),
  logout: () => jsend('/api/ui/logout', {}),
  bootstrap: (): Promise<Bootstrap> => jsend('/api/ui/bootstrap'),
  action: (act: string) => jsend('/api/ui/action', { act }),
  apply: (payload: any) => jsend('/api/ui/apply', payload),
  terminal: (payload: any) => jsend('/api/ui/terminal', payload),
  stickyToggle: () => jsend('/api/ui/sticky-toggle', {}),
  stickySettings: (payload: any) => jsend('/api/ui/sticky-settings', payload),
  stickyRelease: (task_id: string) => jsend('/api/ui/sticky-release', { task_id }),
  stickyRotate: (task_id: string) => jsend('/api/ui/sticky-rotate', { task_id }),
};

export function copyText(text: string): Promise<void> {
  return navigator.clipboard.writeText(text).catch(() => {});
}