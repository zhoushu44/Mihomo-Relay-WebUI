import { useState, useEffect, useCallback, createContext, useContext } from 'react';
import { Routes, Route, NavLink, useNavigate, useLocation } from 'react-router-dom';
import { api, setStoredKey, clearStoredKey, type Bootstrap } from './api';
import { Toast, useToast } from './components';
import Login from './pages/Login';
import Home from './pages/Home';
import Connect from './pages/Connect';
import ApiKey from './pages/ApiKey';
import ApiDocs from './pages/ApiDocs';

interface Ctx {
  data: Bootstrap | null;
  authed: boolean;
  toast: string | null;
  notify: (msg: string) => void;
  refresh: () => Promise<void>;
  onLogout: () => Promise<void>;
}
const AppCtx = createContext<Ctx>(null as any);
export const useApp = () => useContext(AppCtx);

function Header() {
  const { data, onLogout } = useApp();
  return (
    <header className="hdr">
      <div className="hdr-in">
        <div className="brand">
          <a className="brand-link" href="#/">
            <div className="logo">M</div>
            <div>
              <div className="brand-name">Mihomo Relay</div>
              <div className="brand-sub">SOCKS5/HTTP 智能代理中转平台</div>
            </div>
          </a>
          <nav className="nav">
            <NavLink to="/" end className={({ isActive }) => (isActive ? 'active' : '')}>首页</NavLink>
            <NavLink to="/connect" className={({ isActive }) => (isActive ? 'active' : '')}>连接配置</NavLink>
            <NavLink to="/apikey" className={({ isActive }) => (isActive ? 'active' : '')}>API Key</NavLink>
            <NavLink to="/docs" className={({ isActive }) => (isActive ? 'active' : '')}>API 文档</NavLink>
          </nav>
        </div>
        <div className="hdr-right">
          <span className="status-chip">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" fill={data?.status.alive ? '#16a34a' : '#dc2626'} /></svg>
            {data?.status.alive ? '代理正常' : '代理异常'}
          </span>
          <button className="btn-text" onClick={onLogout}>退出登录</button>
        </div>
      </div>
    </header>
  );
}

function Shell() {
  const { data, toast } = useApp();
  return (
    <div>
      <Header />
      <main className="main">
        <Routes>
          <Route path="/" element={<Home data={data!} />} />
          <Route path="/connect" element={<Connect data={data!} />} />
          <Route path="/apikey" element={<ApiKey data={data!} />} />
          <Route path="/docs" element={<ApiDocs />} />
        </Routes>
      </main>
      <footer>Mihomo Relay · SOCKS5 · HTTP · 粘性会话智能中转平台</footer>
      <Toast msg={toast} />
    </div>
  );
}

export default function App() {
  const [data, setData] = useState<Bootstrap | null>(null);
  const [authed, setAuthed] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);
  const [toast, notify] = useToast();
  const navigate = useNavigate();
  const location = useLocation();

  const refresh = useCallback(async () => {
    try {
      const d = await api.bootstrap();
      if (d && d.ok) {
        setData(d);
        setAuthed(true);
        return;
      }
      if (d && (d.error === 'unauthorized' || d.error === 'key_not_configured')) clearStoredKey();
    } catch { /* 网络/解析错误 */ }
    setAuthed(false);
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  const onLogout = async () => {
    await api.logout().catch(() => {});
    clearStoredKey();
    setAuthed(false);
    setData(null);
    navigate('/');
  };

  if (loading) return <div className="login-wrap"><div>加载中…</div></div>;
  if (!authed || !data) {
    // API 文档页公开：未登录也可查看
    if (location.pathname.startsWith('/docs')) {
      return (
        <div>
          <header className="hdr">
            <div className="hdr-in">
              <div className="brand">
                <a className="brand-link" href="#/">
                  <div className="logo">M</div>
                  <div>
                    <div className="brand-name">Mihomo Relay</div>
                    <div className="brand-sub">API 文档（公开）</div>
                  </div>
                </a>
                <nav className="nav">
                  <NavLink to="/docs" className={({ isActive }) => (isActive ? 'active' : '')}>API 文档</NavLink>
                  <a href="#/" className="btn-text" style={{ marginLeft: 8 }}>返回登录</a>
                </nav>
              </div>
            </div>
          </header>
          <main className="main"><ApiDocs /></main>
          <footer>Mihomo Relay · API 文档公开可查 · 管理接口需 API Key</footer>
          <Toast msg={toast} />
        </div>
      );
    }
    return <Login onLogin={async (key) => {
      try {
        const r = await api.login(key);
        if (r?.ok) {
          setStoredKey(key);
          await refresh();
          return true;
        }
      } catch { /* fallthrough */ }
      return false;
    }} />;
  }

  return (
    <AppCtx.Provider value={{ data, authed, toast, notify, refresh, onLogout }}>
      <Shell />
    </AppCtx.Provider>
  );
}