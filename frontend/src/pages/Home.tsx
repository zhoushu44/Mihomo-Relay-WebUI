import { useState } from 'react';
import { api, type Bootstrap, type Session } from '../api';
import { copyText } from '../api';
import { useApp } from '../App';

function CopyValue({ value }: { value: string }) {
  const { notify } = useApp();
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      <input className="input mono" value={value} readOnly style={{ flex: 1, minWidth: 240, background: '#f8fafc' }} />
      <button
        className="btn-primary btn-small"
        onClick={() => { copyText(value); notify('已复制'); }}
      >复制</button>
    </div>
  );
}

function SessionsCard({ sessions, onRefresh }: { sessions: Session[]; onRefresh: () => void }) {
  const { notify } = useApp();
  const [busy, setBusy] = useState('');
  const act = async (fn: () => Promise<any>, id: string) => {
    if (busy) return;
    setBusy(id);
    const r = await fn().catch(() => ({ ok: false, message: '请求失败' }));
    notify(r?.message || '完成');
    setBusy('');
    onRefresh();
  };
  if (!sessions.length) {
    return <div className="section-label" style={{ color: '#94a3b8' }}>暂无活跃会话</div>;
  }
  return (
    <div style={{ marginTop: 12, overflowX: 'auto' }}>
      <table className="mini-table">
        <thead><tr><th>task_id</th><th>绑定代理</th><th>端口</th><th>场景</th><th>过期时间</th><th>操作</th></tr></thead>
        <tbody>
          {sessions.map((s) => (
            <tr key={s.task_id}>
              <td style={{ fontFamily: 'monospace' }}>{s.task_id}</td>
              <td style={{ fontFamily: 'monospace' }}>{s.proxy}</td>
              <td style={{ fontFamily: 'monospace' }}>{s.listener_port}</td>
              <td>{s.scenario}</td>
              <td>{s.expires_at || '不过期'}</td>
              <td>
                <div className="act-row">
                  <button className="act-mini red" disabled={!!busy} onClick={() => act(() => api.stickyRelease(s.task_id), `r-${s.task_id}`)}>释放</button>
                  <button className="act-mini blue" disabled={!!busy} onClick={() => act(() => api.stickyRotate(s.task_id), `t-${s.task_id}`)}>切换</button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Home({ data }: { data: Bootstrap }) {
  const { notify, refresh } = useApp();
  const [busyAct, setBusyAct] = useState('');
  const [speed, setSpeed] = useState<any>(null);

  const runAction = async (act: string) => {
    if (busyAct) return;
    setBusyAct(act);
    const r = await api.action(act).catch(() => ({ ok: false, message: '请求超时或无响应' }));
    notify(`${r?.message || '完成'}${r?.speed ? `：${r.speed.speed} Mbps` : ''}`);
    if (r?.speed) setSpeed(r.speed);
    if (r?.status) {
      try { await refresh(); } catch { /* ignore */ }
    }
    setBusyAct('');
  };

  const toggleSticky = async () => {
    setBusyAct('sticky');
    const r = await api.stickyToggle().catch(() => ({ ok: false, message: '请求失败' }));
    notify(r?.message || '完成');
    setBusyAct('');
    await refresh();
  };

  const stickyOK = !!(data.settings?.socks?.username || data.settings?.http?.username);
  const scenario = data.settings?.scenario || '';
  const inScenarios = ['A', 'proxy', 'E', 'F'].includes(scenario);
  const s = data.status;

  return (
    <div>
      <section className="page-head">
        <div className="pill-tag">状态概览 · 实时</div>
        <h1 className="page-title">首页</h1>
        <p className="page-desc">服务状态、快速接入指引与粘性会话管理。</p>
      </section>

      {/* 当前状态 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">当前状态</h2>
        </div>
        <div className="stats">
          <div className="stat">
            <div className="k">代理状态</div>
            <div className={`v ${s.alive ? 'grn' : 'red'}`}>{s.alive ? '正常' : '不可用'}</div>
          </div>
          {s.ip && (
            <div className="stat">
              <div className="k">出口 IP</div>
              <div className="v acc" style={{ fontSize: 15 }}>{s.ip}{s.country ? `（${s.country}）` : ''}</div>
            </div>
          )}
          {s.mode && (
            <div className="stat">
              <div className="k">运行模式</div>
              <div className="v amb" style={{ fontSize: 16 }}>{s.mode}</div>
            </div>
          )}
          <div className="stat">
            <div className="k">入站模式</div>
            <div className="v acc" style={{ fontSize: 16 }}>{data.entry_mode_label || '—'}</div>
          </div>
          <div className="stat">
            <div className="k">粘性会话</div>
            <div className={`v ${data.sticky.enabled ? 'grn' : 'red'}`}>{data.sticky.enabled ? '已开启' : '已关闭'}</div>
          </div>
        </div>
        <div className="row-actions">
          <button className="btn-primary" disabled={!!busyAct} onClick={() => runAction('test')}>
            {busyAct === 'test' ? '测试中…' : '测试代理'}
          </button>
          <button className="btn-primary" disabled={!!busyAct} onClick={() => runAction('speed')}>
            {busyAct === 'speed' ? '测速中…' : '测速'}
          </button>
          <button className="btn-text" disabled={!!busyAct} onClick={() => runAction('restart')}>
            {busyAct === 'restart' ? '重启中…' : '重启 mihomo'}
          </button>
          <button className="btn-text" disabled={!!busyAct} onClick={() => runAction('refresh')}>
            {busyAct === 'refresh' ? '刷新中…' : '立即刷新代理'}
          </button>
        </div>
        {speed && (
          <div style={{ marginTop: 12, fontSize: 12, color: '#475569' }}>
            测速: <b>{speed.speed} Mbps</b> · 延迟 {speed.latency} ms
            <div style={{ marginTop: 6, height: 8, borderRadius: 4, background: '#eef2ff', overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${speed.bar_width}%`, background: 'linear-gradient(90deg,#818cf8,#4f46e5)', borderRadius: 4 }} />
            </div>
          </div>
        )}
      </section>

      {/* 快速开始 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">快速开始</h2>
          <span className="card-sub">3 步接入</span>
        </div>
        <div className="steps">
          <div className={`step ${stickyOK ? 'done' : ''}`}>
            <span className="n">{stickyOK ? '✓' : '1'}</span>
            <div className="d"><b>设置对外入口账号密码</b> — 到「连接配置」的对外连接填写入口用户名/密码并保存，这是客户端连接时的认证凭据。</div>
          </div>
          <div className={`step ${inScenarios ? 'done' : ''}`}>
            <span className="n">{inScenarios ? '✓' : '2'}</span>
            <div className="d"><b>选择场景并保存应用</b> — 按你的上游来源选一个场景（挂代理 / API 提取 / 订阅 / 直连），点「保存应用」生效。</div>
          </div>
          <div className={`step ${data.conn_ready ? 'done' : ''}`}>
            <span className="n">{data.conn_ready ? '✓' : '3'}</span>
            <div className="d"><b>复制连接链接使用</b> — 用下面的链接接入，或用「API Key」页签的接口做程序化接入。</div>
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 14 }}>
          {data.conn_urls.socks.enabled && (
            <div>
              <div className="section-label">SOCKS5 链接</div>
              <CopyValue value={data.conn_urls.socks.masked_url} />
            </div>
          )}
          {data.conn_urls.http.enabled && (
            <div>
              <div className="section-label">HTTP 链接</div>
              <CopyValue value={data.conn_urls.http.masked_url} />
            </div>
          )}
        </div>
        <div className="note">密码含特殊字符（如 <code>@</code>）已自动 URL 编码，复制后直接可用。若入口账号密码未设置，链接将不包含认证信息。</div>
      </section>

      {/* 端口速查 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">端口速查</h2>
        </div>
        <table className="mini-table">
          <thead><tr><th>端口</th><th>用途</th><th>当前状态</th></tr></thead>
          <tbody>
            <tr><td>7890</td><td>对外入口（{data.entry_mode_label}）</td><td className={data.settings?.socks?.enabled ? '' : 'dim'}>{data.settings?.socks?.enabled ? '已开启' : '未启用'}</td></tr>
            {data.settings?.entry_mode === 'dual' && <tr><td>7891</td><td>HTTP 对外入口</td><td>{data.settings?.http?.enabled ? '已开启' : '未启用'}</td></tr>}
            <tr><td>7892</td><td>WebUI 管理面板 + API 接口</td><td>固定</td></tr>
            <tr><td>40001-40999</td><td>粘性会话动态端口（{data.sticky.enabled ? '已开启' : '未开启'}）</td><td>{data.sticky.enabled ? '按需分配' : '—'}</td></tr>
          </tbody>
        </table>
        <div className="note">端口需在云安全组与服务器防火墙同时放行。详见「API Key」页签或 README。</div>
      </section>

      {/* 粘性会话 */}
      <section className="card accent">
        <div className="card-hd">
          <h2 className="card-title">粘性会话模式 <span className="card-sub">端口 40001-40999</span></h2>
          <button className={`${data.sticky.enabled ? 'btn-text' : 'btn-primary'} btn-small`} disabled={!!busyAct} onClick={toggleSticky}>
            {busyAct === 'sticky' ? '处理中…' : (data.sticky.enabled ? '关闭粘性会话模式' : '开启粘性会话模式')}
          </button>
        </div>

        <div className="stats">
          <div className="stat"><div className="k">代理池总数</div><div className="v acc">{data.pool.total}</div></div>
          <div className="stat"><div className="k">可用节点</div><div className="v grn">{data.pool.available}</div></div>
          <div className="stat"><div className="k">占用节点</div><div className="v amb">{data.pool.in_use}</div></div>
          <div className="stat"><div className="k">活跃会话</div><div className="v">{data.sessions.length}</div></div>
        </div>

        <div className="section-label">活跃会话</div>
        <SessionsCard sessions={data.sessions} onRefresh={async () => { try { await refresh(); } catch { /* ignore */ } }} />

        <div className="note" style={{ marginTop: 14 }}>
          粘性模式开启后，每个 <code>task_id</code> 通过 <code>/api/v1/proxy?session=xxx</code> 申请独立端口（40001-40999），同 session 请求幂等共用同一 IP。E 场景下为「1 请求 1 IP」；F 直连不过期。
        </div>
      </section>
    </div>
  );
}