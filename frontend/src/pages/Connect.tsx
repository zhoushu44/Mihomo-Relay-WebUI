import { useState } from 'react';
import { api, type Bootstrap } from '../api';
import { useApp } from '../App';

function SaveBtn({ children, disabled }: { children: React.ReactNode; disabled?: boolean }) {
  return <button className="btn-primary" type="submit" disabled={disabled}>{children}</button>;
}

export default function Connect({ data }: { data: Bootstrap }) {
  const { notify, refresh } = useApp();
  const settings = data.settings || {};
  const saved = settings.saved_scenarios || {};
  const [busy, setBusy] = useState('');
  const [entryMode, setEntryMode] = useState(settings.entry_mode || 'mixed');
  const [entryPort, setEntryPort] = useState(String(settings.socks?.port ?? 7890));
  const [entryUser, setEntryUser] = useState(settings.socks?.username || '');
  const [entryPass, setEntryPass] = useState('');
  const isDual = entryMode === 'dual';
  const uniTitle = entryMode === 'http' ? 'HTTP 入口' : entryMode === 'socks' ? 'SOCKS5 入口' : '混合单端口';
  const curScenario = settings.scenario || '';

  const runAPI = async (fn: () => Promise<any>) => {
    setBusy('all');
    const r = await fn().catch(() => ({ ok: false, message: '请求失败' }));
    notify(r?.message || '完成');
    setBusy('');
    await refresh();
  };

  const saveTerminal = (e: React.FormEvent) => {
    e.preventDefault();
    const payload: any = { entry_mode: entryMode };
    if (isDual) {
      payload.socks_enabled = (e.target as any).socks_enabled?.checked ? 'on' : '' ;
      payload.socks_port = (e.target as any).socks_port?.value;
      payload.socks_username = (e.target as any).socks_username?.value;
      payload.socks_password = (e.target as any).socks_password?.value;
      payload.http_enabled = (e.target as any).http_enabled?.checked ? 'on' : '';
      payload.http_port = (e.target as any).http_port?.value;
      payload.http_username = (e.target as any).http_username?.value;
      payload.http_password = (e.target as any).http_password?.value;
    } else {
      payload.entry_port = entryPort;
      payload.entry_username = entryUser;
      payload.entry_password = entryPass;
    }
    runAPI(() => api.terminal(payload));
  };

  const apply = (e: React.FormEvent, scenario: string) => {
    e.preventDefault();
    const form = e.target as HTMLFormElement;
    const fd = new FormData(form);
    const payload: any = { scenario };
    if (scenario === 'proxy') {
      payload.proxy_type = form.proxy_type?.value || 'socks5';
      payload.proxies = (form.proxies as HTMLTextAreaElement).value;
      payload.username = (form.username as HTMLInputElement).value;
      payload.password = (form.password as HTMLInputElement).value;
      payload.rotate = form.rotate?.value || 'yes';
    } else if (scenario === 'E') {
      payload.api_url = (form.api_url as HTMLInputElement).value;
      payload.api_num = (form.api_num as HTMLInputElement).value;
    } else if (scenario === 'F') {
      payload.clash_url = (form.clash_url as HTMLInputElement).value;
      payload.f_mode = form.f_mode?.value || 'direct';
    } else if (scenario === 'A') {
      payload.configured = true;
    }
    void fd;
    runAPI(() => api.apply(payload));
  };

  const switchScenario = (scenario: string) => runAPI(() => api.apply({ scenario, switch: '1' }));

  const proxyParams = saved.proxy || {};
  const eParams = saved.E || {};
  const fParams = saved.F || {};

  return (
    <div>
      <section className="page-head">
        <div className="pill-tag">SOCKS5 · HTTP · 上游场景</div>
        <h1 className="page-title">连接配置</h1>
        <p className="page-desc">配置对外入口（SOCKS5/HTTP）与上游代理来源场景。</p>
      </section>

      {/* 对外连接 */}
      <section className="card accent">
        <div className="card-hd">
          <h2 className="card-title">对外连接</h2>
          <span className="card-sub">客户端连接的入口</span>
        </div>
        <form onSubmit={saveTerminal}>
          <div className="grid2">
            <label className="field">
              入口模式
              <select className="input" name="entry_mode" value={entryMode} onChange={(e) => setEntryMode(e.target.value)}>
                <option value="mixed">混合单端口（SOCKS5 / HTTP 通用）</option>
                <option value="socks">仅 SOCKS5</option>
                <option value="http">仅 HTTP</option>
                <option value="dual">双入口（SOCKS5 + HTTP 独立）</option>
              </select>
            </label>
            {!isDual && (
              <>
                <label className="field">
                  {uniTitle}端口
                  <input className="input" type="number" name="entry_port" value={entryPort} min={1} max={65535} onChange={(e) => setEntryPort(e.target.value)} />
                </label>
                <label className="field">
                  用户名
                  <input className="input" name="entry_username" value={entryUser} onChange={(e) => setEntryUser(e.target.value)} autoComplete="off" placeholder="留空则不要求认证" />
                </label>
                <label className="field">
                  密码
                  <input className="input" type="password" name="entry_password" value={entryPass} onChange={(e) => setEntryPass(e.target.value)} autoComplete="new-password" placeholder={settings.socks?.password ? '已设置（留空保持不变）' : '请输入'} />
                </label>
              </>
            )}
          </div>

          {isDual && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 14 }}>
              {(['socks', 'http'] as const).map((kind) => {
                const it = settings[kind] || {};
                return (
                  <div key={kind} style={{ border: '1px solid #e2e8f0', borderRadius: 10, padding: 12 }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, fontWeight: 700 }}>
                      <input type="checkbox" name={`${kind}_enabled`} defaultChecked={it.enabled} style={{ width: 15, height: 15, accentColor: '#4f46e5' }} />
                      {kind === 'socks' ? 'SOCKS5' : 'HTTP'} 入口
                    </label>
                    <div className="grid2" style={{ marginTop: 8 }}>
                      <label className="field">端口<input className="input" name={`${kind}_port`} defaultValue={it.port} /></label>
                      <label className="field">用户名<input className="input" name={`${kind}_username`} defaultValue={it.username} /></label>
                      <label className="field">密码<input className="input" type="password" name={`${kind}_password`} placeholder={it.password ? '已设置（留空不变）' : ''} /></label>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <div className="row-actions">
            <SaveBtn disabled={!!busy}>保存对外连接设置</SaveBtn>
          </div>
        </form>
        <div className="note">入口账号密码即客户端连接时的认证凭据；混合单端口下同一个端口自动识别 SOCKS5/HTTP。</div>
      </section>

      {/* 场景 A */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">场景 A：直连（无上游代理）</h2>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {curScenario === 'A' && <span className="tag grn">当前</span>}
          </div>
        </div>
        <p className="page-desc" style={{ fontSize: 13 }}>不使用上游代理，服务器直连出口。适合测试或不需要中继的场景。</p>
        <div className="row-actions">
          <button className="btn-primary btn-small" disabled={!!busy} onClick={() => runAPI(() => api.apply({ scenario: 'A' }))}>应用直连模式</button>
        </div>
      </section>

      {/* 场景 B/C/D 挂代理 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">场景 B/C/D：挂代理（SOCKS5 / HTTP）</h2>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {curScenario === 'proxy' && <span className="tag grn">当前</span>}
          </div>
        </div>
        <form onSubmit={(e) => apply(e, 'proxy')}>
          <div className="grid2">
            <label className="field">
              代理类型
              <select className="input" name="proxy_type" defaultValue={proxyParams.proxy_type || 'socks5'}>
                <option value="socks5">SOCKS5</option>
                <option value="http">HTTP</option>
              </select>
            </label>
            <label className="field">
              轮换模式
              <select className="input" name="rotate" defaultValue={proxyParams.rotate || 'yes'}>
                <option value="yes">轮换（每次请求随机出口）</option>
                <option value="no">固定（不轮换）</option>
              </select>
            </label>
          </div>
          <label className="field" style={{ marginTop: 10 }}>
            代理列表（每行一个：host:port）
            <textarea className="input" name="proxies" rows={5} defaultValue={proxyParams.proxies || ''} placeholder={'1.2.3.4:8080\n5.6.7.8:3128'} />
          </label>
          <div className="grid2">
            <label className="field">上游用户名（可选）<input className="input" name="username" defaultValue={proxyParams.username || ''} /></label>
            <label className="field">上游密码（可选）<input className="input" type="password" name="password" defaultValue={proxyParams.password || ''} /></label>
          </div>
          <div className="row-actions">
            <SaveBtn disabled={!!busy}>保存应用</SaveBtn>
          </div>
        </form>
      </section>

      {/* 场景 E */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">场景 E：API 提取（用到才提取）</h2>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {curScenario === 'E' && <span className="tag grn">当前</span>}
            {saved.E && <button className="btn-text btn-small" disabled={!!busy} onClick={() => switchScenario('E')}>切换</button>}
          </div>
        </div>
        <div className={`note ${curScenario === 'E' ? '' : 'warn'}`}>
          <b>前提</b>: 服务器 IP 必须在上游代理平台加白<br />
          <b>机制</b>: {data.sticky.enabled ? '粘性开启 - acquire 时懒加载提取1个新代理，1任务1IP，10分钟过期，失败不自动切换' : '每 2 分钟检测一次，代理过期才提取，不浪费额度'}
        </div>
        <form onSubmit={(e) => apply(e, 'E')}>
          <label className="field">
            API URL
            <input className="input mono" name="api_url" defaultValue={eParams.api_url || ''} placeholder="https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt" />
          </label>
          <label className="field" style={{ marginTop: 10 }}>
            提取数量
            <input className="input" type="number" name="api_num" defaultValue={eParams.api_num || '1'} min={1} max={50} style={{ width: 160 }} />
          </label>
          <div className="row-actions">
            <SaveBtn disabled={!!busy}>保存应用</SaveBtn>
            <button className="btn-text btn-small" type="button" disabled={!!busy} onClick={() => runAPI(() => api.action('refresh'))}>立即刷新代理</button>
          </div>
        </form>
      </section>

      {/* 场景 F */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">场景 F：Clash 订阅链接</h2>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {curScenario === 'F' && <span className="tag grn">当前</span>}
            {saved.F && <button className="btn-text btn-small" disabled={!!busy} onClick={() => switchScenario('F')}>切换</button>}
          </div>
        </div>
        <div className="note">
          <b>用法</b>: 填入 Clash 订阅链接，自动解析为代理列表<br />
          <b>支持</b>: Clash、Clash.Meta、Base64/vmess/vless/trojan/ss/hysteria2 订阅格式<br />
          <b>粘性模式</b>: {data.sticky.enabled ? '直连模式（绑定第一个可用节点，不过期，故障自动切换）/ 轮询模式（10分钟过期）' : '未开启粘性'}
        </div>
        <form onSubmit={(e) => apply(e, 'F')}>
          <label className="field">
            Clash 订阅 URL
            <input className="input mono" name="clash_url" defaultValue={fParams.clash_url || ''} placeholder="https://example.com/sub?token=xxx" />
          </label>
          <label className="field" style={{ marginTop: 10 }}>
            模式
            <select className="input" name="f_mode" defaultValue={fParams.mode !== 'poll' ? 'direct' : 'poll'} style={{ maxWidth: 420 }}>
              <option value="direct">直连模式（粘性，不过期，故障自动切换）</option>
              <option value="poll">轮询模式（粘性，10分钟过期）</option>
            </select>
          </label>
          <div className="row-actions">
            <SaveBtn disabled={!!busy}>保存应用</SaveBtn>
          </div>
        </form>
      </section>

      {/* 当前配置 */}
      <section className="card">
        <div className="card-hd">
          <h2 className="card-title">当前配置文件</h2>
        </div>
        {data.config_text ? (
          <pre className="codeblock" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{data.config_text}</pre>
        ) : (
          <p className="page-desc" style={{ fontSize: 13 }}>暂无配置（尚未生成）</p>
        )}
      </section>
    </div>
  );
}