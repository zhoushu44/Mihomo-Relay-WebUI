import { useState } from 'react';
import { type Bootstrap, getStoredKey, setStoredKey } from '../api';
import { copyText } from '../api';
import { useApp } from '../App';

function UsageRow({ name, url, desc }: { name: string; url: string; desc: string }) {
  const { notify } = useApp();
  return (
    <div style={{ border: '1px solid #e2e8f0', borderRadius: 10, padding: '10px 14px', marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 6 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: '#4f46e5', background: '#eef2ff', padding: '2px 8px', borderRadius: 6 }}>{name}</span>
        <button
          onClick={() => { copyText(url); notify('已复制'); }}
          style={{ padding: '5px 10px', background: '#f1f5f9', color: '#334155', border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 11, fontWeight: 600 }}
        >复制</button>
      </div>
      <div style={{ fontFamily: 'monospace', fontSize: 12, color: '#475569', wordBreak: 'break-all' }}>{url}</div>
      <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>{desc}</div>
    </div>
  );
}

export default function ApiKey({ data }: { data: Bootstrap }) {
  const { notify, refresh } = useApp();
  const server = data.server || '';
  const apiKey = data.api_key || '';
  const [visible, setVisible] = useState(false);
  const [newKey, setNewKey] = useState('');
  // 提取 IP 区
  const [session, setSession] = useState('');
  const [consume, setConsume] = useState(false);
  const [fmt, setFmt] = useState('txt');
  const [extracting, setExtracting] = useState(false);
  const [result, setResult] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [saveOk, setSaveOk] = useState(false);
  // 配置控制区（/api/v1/config）
  const [cfgBusy, setCfgBusy] = useState(false);
  const [cfgResult, setCfgResult] = useState('');
  const [cfgTab, setCfgTab] = useState<'entry' | 'scenario'>('entry');
  const [cEntryMode, setCEntryMode] = useState('mixed');
  const [cPort, setCPort] = useState('');
  const [cUser, setCUser] = useState('');
  const [cPass, setCPass] = useState('');
  const [cScenario, setCScenario] = useState('A');
  const [cSwitch, setCSwitch] = useState(true);
  const [cProxyType, setCProxyType] = useState('socks5');
  const [cProxies, setCProxies] = useState('');
  const [cApiUrl, setCApiUrl] = useState('');
  const [cApiNum, setCApiNum] = useState('1');
  const [cClashUrl, setCClashUrl] = useState('');

  const cfgGet = async () => {
    if (!apiKey) { setCfgResult('请先在「Key 管理」设置 API Key'); return; }
    setCfgBusy(true);
    try {
      const r = await fetch(`/api/v1/config?key=${encodeURIComponent(apiKey)}`);
      const text = await r.text();
      if (r.ok) {
        try { setCfgResult(JSON.stringify(JSON.parse(text), null, 2)); }
        catch { setCfgResult(text); }
      } else {
        let msg = `服务返回 ${r.status}`;
        try { const d = JSON.parse(text); if (d?.message) msg = d.message; } catch { /* ignore */ }
        setCfgResult(msg);
      }
    } catch { setCfgResult('无法连接服务'); }
    finally { setCfgBusy(false); }
  };

  const cfgPost = async () => {
    if (!apiKey) { setCfgResult('请先在「Key 管理」设置 API Key'); return; }
    setCfgBusy(true);
    let body: any;
    if (cfgTab === 'entry') {
      body = { entry_mode: cEntryMode };
      if (cPort.trim()) body.entry_port = cPort.trim();
      if (cUser.trim()) body.entry_username = cUser.trim();
      if (cPass) body.entry_password = cPass;
    } else {
      body = { scenario: cScenario };
      if (cScenario === 'proxy') {
        body.proxy_type = cProxyType;
        if (cProxies.trim()) body.proxies = cProxies;
        if (cSwitch) body.switch = '1';
      } else if (cScenario === 'E') {
        if (cApiUrl.trim()) body.api_url = cApiUrl.trim();
        body.api_num = cApiNum || '1';
      } else if (cScenario === 'F') {
        if (cClashUrl.trim()) body.clash_url = cClashUrl.trim();
      } else if (cScenario === 'A') {
        if (cSwitch) body.switch = '1';
      }
    }
    try {
      const r = await fetch(`/api/v1/config?key=${encodeURIComponent(apiKey)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const text = await r.text();
      let data: any = null;
      try { data = JSON.parse(text); } catch { /* ignore */ }
      if (r.ok && data?.ok) {
        setCfgResult(`✓ ${data.message || '配置已应用'}`);
        notify(data.message || '配置已应用');
        await refresh();
      } else {
        setCfgResult(`✗ ${data?.message || `服务返回 ${r.status}`}`);
        notify(data?.message || `服务返回 ${r.status}`);
      }
    } catch { setCfgResult('无法连接服务'); notify('无法连接服务'); }
    finally { setCfgBusy(false); }
  };

  // 修改 Key：走 terminal 接口的 api_key 字段
  const saveKey = async () => {
    if (!newKey.trim()) return;
    const r = await new Promise<any>((res) => {
      fetch('/api/ui/terminal', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': getStoredKey() },
        body: JSON.stringify({ api_key: newKey.trim() }),
      }).then((x) => x.json()).then(res);
    });
    if (r?.ok) {
      setStoredKey(newKey.trim()); // 立即切换为新 Key，避免自身旧 Key 失效被登出
      setNewKey('');
      setSaveOk(true);
      setTimeout(() => setSaveOk(false), 1800);
      notify('API Key 已更新');
      await refresh();
    } else {
      notify(r?.message || '更新失败');
    }
  };

  const runExtract = async () => {
    if (!apiKey) { setError('请先设置 API Key'); return; }
    setExtracting(true);
    setError('');
    setResult([]);
    const params = new URLSearchParams();
    params.set('key', apiKey);
    if (session.trim()) params.set('session', session.trim());
    if (consume) params.set('consume', '1');
    if (fmt === 'txt') params.set('format', 'txt');
    try {
      const r = await fetch(`/api/v1/proxy?${params.toString()}`);
      const text = await r.text();
      if (!r.ok) {
        let msg = `服务返回 ${r.status}`;
        try { const d = JSON.parse(text); if (d && d.message) msg = d.message; } catch { /* ignore */ }
        setError(msg);
        return;
      }
      const lines = text.split('\n').map((s) => s.trim()).filter(Boolean);
      setResult(lines);
      notify(lines.length ? '提取成功' : '空结果');
    } catch {
      setError('无法连接服务');
    } finally {
      setExtracting(false);
    }
  };

  const buildUrl = () => {
    const params = new URLSearchParams();
    params.set('key', apiKey);
    if (session.trim()) params.set('session', session.trim());
    if (consume) params.set('consume', '1');
    if (fmt === 'txt') params.set('format', 'txt');
    return `/api/v1/proxy?${params.toString()}`;
  };
  const extractUrl = buildUrl();

  const usageRows = [
    { name: '单条提取', url: `${server}/api/v1/proxy?key=你的KEY`, desc: '返回共享出口连接（non-sticky 环境）或当前活跃入口 URL' },
    { name: '粘性会话', url: `${server}/api/v1/proxy?key=你的KEY&session=会话ID`, desc: '同一 session 固定同一 IP（粘性模式下独占端口，幂等）' },
    { name: '消耗式提取', url: `${server}/api/v1/proxy?key=你的KEY&session=会话ID&consume=1`, desc: '取走即释放该 session 的 IP（注册成功后用，防止同 IP 多账号关联）' },
    { name: '纯文本输出', url: `${server}/api/v1/proxy?key=你的KEY&format=txt`, desc: '直接返回一行 socks5:// 地址，适合命令行' },
    { name: '销毁 IP（注册失败）', url: `${server}/api/v1/proxy/destroy?key=你的KEY&session=会话ID`, desc: '注册失败/放弃时立即销毁该 session 的 IP：释放绑定并交还池' },
    { name: '查看连接配置', url: `${server}/api/v1/config?key=你的KEY`, desc: 'GET：返回当前入口模式、socks/http 端口、场景等连接配置摘要' },
    { name: '修改连接配置', url: `${server}/api/v1/config?key=你的KEY`, desc: 'POST：改入口模式/端口/账号密码，或切换场景（见下方示例）' },
  ];

  const paramRows = [
    { name: 'key', required: '是', desc: 'API Key（通用 Key，即管理面板设置的 Key）。服务未设置 Key 时开放访问，可不带', example: 'a1b2c3d4...' },
    { name: 'session', required: '粘性必填', desc: '会话 ID：同一 session 固定同 IP（独占端口），幂等', example: 'user_001' },
    { name: 'consume', required: '否', desc: 'consume=1 = 精确回收该 session 绑定的 IP（注册成功后用）', example: '1' },
    { name: 'format', required: '否', desc: 'format=txt = 直接返回纯文本地址', example: 'txt' },
  ];

  const errorRows = [
    { status: '401', title: 'Key 无效', desc: 'API Key 无效。服务未设置 Key 时开放访问，不会出现此错误。' },
    { status: '403', title: '未设置 Key 禁止管理', code: 'KEY_NOT_CONFIGURED', desc: '服务未设置 API Key 时，管理接口（/api/v1/config 等）禁止访问；仅提取接口开放。' },
    { status: '400', title: '缺少参数', code: 'BAD_REQUEST', desc: 'POST /api/v1/config 未携带任何可操作的配置字段，或 consume=1 未带 session。' },
    { status: '400', title: '缺少 session 参数', code: 'SESSION_REQUIRED', desc: 'consume=1 / 销毁接口未带 session 参数。' },
    { status: '404', title: '会话不存在', code: 'SESSION_NOT_FOUND', desc: '销毁接口：该 session 不存在或已解除绑定（可忽略，说明已释放）。' },
    { status: '409', title: '粘性会话到期', code: 'SESSION_EXPIRED', desc: '粘性会话已到期（默认 10 分钟），已解除绑定；重试同 session 会自动绑定新 IP。' },
    { status: '409', title: '池内无空闲代理', code: 'POOL_EXHAUSTED', desc: '池内没有空闲代理可绑定，稍后再试。' },
    { status: '400', title: '粘性未开启', code: 'STICKY_DISABLED', desc: '带 session 取代理但粘性会话模式未开启，请先在首页开启粘性模式。' },
  ];

  return (
    <div>
      <section className="page-head">
        <div className="pill-tag">通用 Key 鉴权 · 全局唯一</div>
        <h1 className="page-title">API Key</h1>
        <p className="page-desc">
          这是平台的通用 Key：一套 Key 既管代理提取（<code>/api/v1/proxy</code>），也管连接配置（<code>/api/v1/config</code> 可改入口模式/端口/账号与场景）。支持粘性会话与消耗式提取。服务未设置 Key 时提取接口开放访问，管理接口禁止。
        </p>
      </section>

      {/* Key 管理 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">Key 管理</h2>
          <span className="card-sub">此 Key 同时用于管理鉴权与提取接口</span>
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginTop: 14 }}>
          <input
            className="input mono"
            type={visible ? 'text' : 'password'}
            value={apiKey}
            readOnly
            style={{ flex: 1, minWidth: 260, fontSize: 12 }}
            placeholder="未设置 API Key"
          />
          <button className="btn-text" onClick={() => setVisible(!visible)}>{visible ? '隐藏' : '显示'}</button>
          <button
            className="btn-text"
            disabled={!apiKey}
            onClick={() => { copyText(apiKey); notify('已复制 Key'); }}
          >复制 Key</button>
        </div>
        <div className="section-label">更新 Key（保存后立即生效，旧 Key 失效）</div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <input
            className="input mono"
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder="粘贴新的 API Key"
            style={{ flex: 1, minWidth: 240, fontSize: 12 }}
          />
          <button className="btn-primary" disabled={!newKey.trim()} onClick={saveKey}>
            {saveOk ? '已保存' : '更新 Key'}
          </button>
        </div>
        <div className="note">传递方式：URL 查询参数 <code>?key=你的KEY</code>（GET 请求最方便），也可用请求头 <code>X-API-Key</code>。</div>
      </section>

      {/* 提取 IP */}
      <section className="card accent">
        <div className="card-hd">
          <h2 className="card-title">提取 IP</h2>
          <span className="card-sub">用当前 Key 实时取连接，即统一接口的实际调用</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10, marginTop: 14 }}>
          <label className="field">
            会话 ID（粘性，可选）
            <input className="input" value={session} onChange={(e) => setSession(e.target.value)} placeholder="如 user_001" />
          </label>
          <label className="field">
            输出格式
            <select className="input" value={fmt} onChange={(e) => setFmt(e.target.value)}>
              <option value="txt">纯文本（一行地址）</option>
              <option value="json">JSON</option>
            </select>
          </label>
          <label className="field" style={{ alignItems: 'center', flexDirection: 'row', gap: 8, cursor: 'pointer', alignSelf: 'end', padding: '9px 4px' }}>
            <input type="checkbox" checked={consume} onChange={(e) => setConsume(e.target.checked)} style={{ width: 15, height: 15, accentColor: '#4f46e5' }} />
            消耗式（取走即释放）
          </label>
          <button
            className="btn-primary"
            disabled={extracting}
            onClick={runExtract}
            style={{ alignSelf: 'end', justifyContent: 'center' }}
          >{extracting ? '提取中...' : '提取 IP'}</button>
        </div>
        {error && <div className="err">⚠ {error}</div>}
        {result.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <span style={{ fontSize: 12, color: '#475569', fontWeight: 600 }}>提取到 {result.length} 条：</span>
              <button className="btn-primary btn-small" onClick={() => { copyText(result.join('\n')); notify('已复制'); }}>全部复制</button>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {result.map((proxy, i) => (
                <div className="result-row" key={i}>
                  <span className="val">{proxy}</span>
                  <button style={{ padding: '5px 10px', background: '#1e293b', color: '#e2e8f0', border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 11 }} onClick={() => { copyText(proxy); notify('已复制'); }}>复制</button>
                </div>
              ))}
            </div>
          </div>
        )}
        <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <input className="input mono" value={extractUrl} readOnly style={{ flex: 1, minWidth: 260, fontSize: 11, background: '#f8fafc' }} />
          <button className="btn-text" onClick={() => { copyText(extractUrl); notify('已复制请求 URL'); }}>复制请求 URL</button>
        </div>
      </section>

      {/* 控制连接配置 */}
      <section className="card accent">
        <div className="card-hd">
          <h2 className="card-title">控制连接配置</h2>
          <span className="card-sub">通过 <code>/api/v1/config</code> 用 Key 直接改对外入口与场景</span>
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginTop: 14 }}>
          <button className="btn-text" disabled={cfgBusy} onClick={cfgGet}>获取当前配置</button>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginLeft: 'auto' }}>
            <div style={{ display: 'flex', background: '#f1f5f9', borderRadius: 8, padding: 2 }}>
              <button
                className={cfgTab === 'entry' ? 'chip active' : 'chip'}
                style={{ border: 'none', padding: '6px 14px', borderRadius: 6, cursor: 'pointer', fontSize: 12, fontWeight: 600 }}
                onClick={() => setCfgTab('entry')}
              >入口配置</button>
              <button
                className={cfgTab === 'scenario' ? 'chip active' : 'chip'}
                style={{ border: 'none', padding: '6px 14px', borderRadius: 6, cursor: 'pointer', fontSize: 12, fontWeight: 600 }}
                onClick={() => setCfgTab('scenario')}
              >场景切换</button>
            </div>
          </div>
        </div>

        {cfgTab === 'entry' ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10, marginTop: 14 }}>
            <label className="field">
              入口模式
              <select className="input" value={cEntryMode} onChange={(e) => setCEntryMode(e.target.value)}>
                <option value="mixed">混合单端口（SOCKS5/HTTP）</option>
                <option value="socks">仅 SOCKS5</option>
                <option value="http">仅 HTTP</option>
                <option value="dual">双入口（SOCKS5 + HTTP 独立）</option>
              </select>
            </label>
            <label className="field">
              端口
              <input className="input mono" value={cPort} onChange={(e) => setCPort(e.target.value)} placeholder="如 7890（不填则保留）" />
            </label>
            <label className="field">
              用户名
              <input className="input mono" value={cUser} onChange={(e) => setCUser(e.target.value)} placeholder="如 user（不填则保留）" />
            </label>
            <label className="field">
              密码
              <input className="input mono" type="password" value={cPass} onChange={(e) => setCPass(e.target.value)} placeholder="新密码（不填则保留）" />
            </label>
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10, marginTop: 14 }}>
            <label className="field">
              场景
              <select className="input" value={cScenario} onChange={(e) => setCScenario(e.target.value)}>
                <option value="A">A：直连（无上游代理）</option>
                <option value="proxy">B/C/D：挂代理（SOCKS5/HTTP）</option>
                <option value="E">E：API 提取</option>
                <option value="F">F：Clash 订阅</option>
              </select>
            </label>
            {cScenario === 'proxy' && (
              <>
                <label className="field">
                  代理类型
                  <select className="input" value={cProxyType} onChange={(e) => setCProxyType(e.target.value)}>
                    <option value="socks5">SOCKS5</option>
                    <option value="http">HTTP</option>
                  </select>
                </label>
                <label className="field" style={{ gridColumn: '1 / -1' }}>
                  代理列表（每行一个 host:port）
                  <textarea className="input mono" rows={3} value={cProxies} onChange={(e) => setCProxies(e.target.value)} placeholder={'1.2.3.4:1080\n5.6.7.8:1080'} />
                </label>
              </>
            )}
            {cScenario === 'E' && (
              <>
                <label className="field" style={{ gridColumn: '1 / -1' }}>
                  API URL
                  <input className="input mono" value={cApiUrl} onChange={(e) => setCApiUrl(e.target.value)} placeholder="https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt" />
                </label>
                <label className="field">
                  提取数量
                  <input className="input mono" value={cApiNum} onChange={(e) => setCApiNum(e.target.value)} placeholder="1" />
                </label>
              </>
            )}
            {cScenario === 'F' && (
              <label className="field" style={{ gridColumn: '1 / -1' }}>
                Clash 订阅链接
                <input className="input mono" value={cClashUrl} onChange={(e) => setCClashUrl(e.target.value)} placeholder="https://example.com/sub" />
              </label>
            )}
            <label className="field" style={{ alignItems: 'center', flexDirection: 'row', gap: 8, cursor: 'pointer', alignSelf: 'end', padding: '9px 4px' }}>
              <input type="checkbox" checked={cSwitch} onChange={(e) => setCSwitch(e.target.checked)} style={{ width: 15, height: 15, accentColor: '#4f46e5' }} />
              仅切换已保存配置
            </label>
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginTop: 14 }}>
          <button className="btn-primary" disabled={cfgBusy} onClick={cfgPost} style={{ justifyContent: 'center' }}>
            {cfgBusy ? '应用中...' : (cfgTab === 'entry' ? '应用入口配置' : '应用场景配置')}
          </button>
        </div>
        {cfgResult && (
          <div className="codeblock" style={{ marginTop: 14, maxHeight: 260, overflow: 'auto' }}>
            <pre style={{ margin: 0 }}>{cfgResult}</pre>
          </div>
        )}
      </section>

      {/* 参数化提取 URL */}
      <section className="card accent">
        <div className="card-hd">
          <h2 className="card-title">参数化提取 URL</h2>
          <span className="card-sub">一个接口 + 参数覆盖所有场景</span>
        </div>
        <div className="table-wrap" style={{ marginTop: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '100px 90px 1fr 140px', gap: 8, padding: '9px 12px', background: '#f8fafc', borderBottom: '1px solid #e2e8f0', color: '#64748b', fontWeight: 700, fontSize: 12 }}>
            <div>参数</div><div>必填</div><div>说明</div><div>示例值</div>
          </div>
          {paramRows.map((row) => (
            <div key={row.name} style={{ display: 'grid', gridTemplateColumns: '100px 90px 1fr 140px', gap: 8, padding: '9px 12px', borderBottom: '1px solid #eef2f7', color: '#334155', fontSize: 12, alignItems: 'center' }}>
              <div style={{ fontFamily: 'monospace', color: '#4f46e5', fontWeight: 600 }}>{row.name}</div>
              <div style={{ color: row.required === '否' ? '#64748b' : '#dc2626' }}>{row.required}</div>
              <div style={{ color: '#475569' }}>{row.desc}</div>
              <div style={{ fontFamily: 'monospace', color: '#64748b' }}>{row.example}</div>
            </div>
          ))}
        </div>
      </section>

      {/* 接口使用说明 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">接口使用说明</h2>
          <span className="card-sub">GET 请求即可，无需额外请求头（Key 已在 URL 中）</span>
        </div>
        <div style={{ marginTop: 14 }}>
          {usageRows.map((row) => <UsageRow key={row.name} {...row} />)}
        </div>
        <div className="section-label">curl 示例</div>
        <div className="codeblock">
          <div><span className="cm"># 注册流程开始：拿粘性代理（同 session 同 IP）</span></div>
          <div>curl "<span className="hl">{server}/api/v1/proxy?key=你的KEY&amp;session=user_001</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 注册成功：consume=1 烧掉该 IP，池子自动补新</span></div>
          <div>curl "<span className="hl">{server}/api/v1/proxy?key=你的KEY&amp;session=user_001&amp;consume=1</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 注册失败：销毁该异常 IP</span></div>
          <div>curl "<span className="hl">{server}/api/v1/proxy/destroy?key=你的KEY&amp;session=user_001</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 纯文本取共享出口</span></div>
          <div>curl "<span className="hl">{server}/api/v1/proxy?key=你的KEY&amp;format=txt</span>"</div>
          <div style={{ marginTop: 10 }}><span className="cm"># 查看当前连接配置</span></div>
          <div>curl "<span className="hl">{server}/api/v1/config?key=你的KEY</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 改入口：混合模式 + 端口 7899 + 账号密码</span></div>
          <div>curl -X POST "<span className="hl">{server}/api/v1/config?key=你的KEY</span>" \<br />&nbsp;&nbsp;-H "Content-Type: application/json" -d {'{"entry_mode":"mixed","entry_port":"7899","entry_username":"user1","entry_password":"pass1"}'}</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 切换场景：直连模式（A）</span></div>
          <div>curl -X POST "<span className="hl">{server}/api/v1/config?key=你的KEY</span>" -H "Content-Type: application/json" -d {'{"scenario":"A","switch":"1"}'}</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 保存并应用场景 E（API 提取）</span></div>
          <div>curl -X POST "<span className="hl">{server}/api/v1/config?key=你的KEY</span>" -H "Content-Type: application/json" -d {'{"scenario":"E","api_url":"https://api.example.com?num=10","api_num":"1"}'}</div>
        </div>
        <div className="note" style={{ marginTop: 14 }}>
          <b>推荐用法（注册场景）：</b>一个账号 = 一个 session = 一个 IP。整个注册流程用同一个 session 取代理（保证同 IP），注册成功后再 <code>consume=1</code> 烧掉该 IP；注册失败/放弃则调用 <code>/api/v1/proxy/destroy?session=xxx</code> 立即销毁该 IP，防止同 IP 多账号关联。
        </div>
      </section>

      {/* 错误响应 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">错误响应</h2>
          <span className="card-sub">请求失败返回 JSON，按 HTTP 状态码与 code 字段处理</span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 14 }}>
          {errorRows.map((row) => (
            <div key={row.title} style={{ border: '1px solid #e2e8f0', borderRadius: 10, padding: '10px 14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 4 }}>
                <span style={{ padding: '2px 8px', background: '#fee2e2', color: '#b91c1c', borderRadius: 6, fontSize: 11, fontWeight: 700, fontFamily: 'monospace' }}>{row.status}</span>
                <span style={{ fontSize: 12, fontWeight: 700, color: '#334155' }}>{row.title}</span>
                {row.code && <span style={{ padding: '2px 8px', background: '#f1f5f9', color: '#64748b', borderRadius: 6, fontSize: 11, fontFamily: 'monospace' }}>{row.code}</span>}
              </div>
              <div style={{ fontSize: 12, color: '#475569', lineHeight: 1.7 }}>{row.desc}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}