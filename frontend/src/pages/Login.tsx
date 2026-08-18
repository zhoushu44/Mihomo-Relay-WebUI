import { useState } from 'react';

export default function Login({ onLogin }: { onLogin: (key: string) => Promise<boolean> }) {
  const [key, setKey] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr('');
    const ok = await onLogin(key.trim());
    if (!ok) setErr('API Key 无效');
    setBusy(false);
  };

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-logo">M</div>
        <div className="login-title">Mihomo Relay WebUI</div>
        <div className="login-sub">SOCKS5/HTTP 智能代理中转平台 · 输入 API Key 登录</div>
        <form onSubmit={submit}>
          <label className="field" style={{ marginBottom: 14 }}>
            API Key
            <input
              className="input"
              type="text"
              name="key"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="请输入平台 API Key"
              autoFocus
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <button className="btn-primary" type="submit" disabled={busy} style={{ width: '100%', justifyContent: 'center', padding: '11px' }}>
            {busy ? '登录中…' : '登录'}
          </button>
          {err && <div className="err">⚠ {err}</div>}
        </form>
        <a href="#/docs" className="btn-text" style={{ display: 'block', textAlign: 'center', marginTop: 14 }}>查看公开 API 文档</a>
      </div>
    </div>
  );
}