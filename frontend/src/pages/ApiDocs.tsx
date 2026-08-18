import { type ReactNode } from 'react';

const BASE = `http://${window.location.host}`;

function T({ children }: { children: ReactNode }) {
  return <div className="table-wrap"><table className="tbl">{children}</table></div>;
}

export default function ApiDocs() {
  const endpoints = [
    {
      method: 'GET', name: '提取代理', path: '/api/v1/proxy',
      desc: '获取可用代理连接。不传 session 返回共享出口；传 session 走粘性会话（同一 session 固定同一 IP）；consume=1 取走即释放。',
      params: [
        ['key', '否*', 'API Key（*服务未设置 Key 时开放，可不带）'],
        ['session', '粘性必填', '会话 ID，同一 session 幂等绑定同一 IP'],
        ['consume', '否', 'consume=1 = 注册成功后烧掉该 IP，池子自动补新'],
        ['format', '否', 'txt = 纯文本一行地址；默认 JSON'],
      ],
    },
    {
      method: 'GET', name: '销毁 IP', path: '/api/v1/proxy/destroy',
      desc: '注册失败/放弃时销毁该 session 的 IP：解除绑定并交还池。',
      params: [
        ['key', '否*', 'API Key'],
        ['session', '是', '要销毁的会话 ID'],
      ],
    },
    {
      method: 'GET', name: '查看连接配置', path: '/api/v1/config',
      desc: '返回当前入口模式、socks/http 端口与账号、场景等连接配置摘要（密码脱敏）。管理面接口，需 Key。',
      params: [['key', '是', 'API Key（服务未设置 Key 时管理接口禁止访问）']],
    },
    {
      method: 'POST', name: '控制连接配置', path: '/api/v1/config',
      desc: '用 Key 直接控制连接配置：改入口模式/端口/账号密码、保存或切换场景、更新 API Key。未提供的字段保留原值。管理面接口，需 Key。',
      params: [
        ['entry_mode', '否', 'mixed / socks / http / dual（改入口时用）'],
        ['entry_port / entry_username / entry_password', '否', '单入口模式下的端口与账号密码'],
        ['scenario', '否', 'A（直连）/ proxy（挂代理）/ E（API 提取）/ F（Clash 订阅）'],
        ['switch', '否', 'switch=1 = 仅切换已保存配置，不保存新参数'],
        ['proxy_type / proxies / username / password', '否', 'scenario=proxy 时：socks5|http、每行一个 host:port 等'],
        ['api_url / api_num', '否', 'scenario=E 时：API 提取链接与数量'],
        ['clash_url', '否', 'scenario=F 时：Clash 订阅链接'],
        ['api_key', '否', '更新平台 API Key'],
      ],
    },
  ];

  const errors = [
    [401, 'UNAUTHORIZED', 'API Key 无效或未提供'],
    [403, 'KEY_NOT_CONFIGURED', '服务未设置 API Key，管理接口禁止访问（提取接口仍开放）'],
    [400, 'BAD_REQUEST', 'POST /api/v1/config 未携带任何可操作字段'],
    [400, 'SESSION_REQUIRED', 'consume=1 / 销毁接口未带 session 参数'],
    [400, 'STICKY_DISABLED', '带 session 取代理但粘性会话模式未开启'],
    [404, 'SESSION_NOT_FOUND', '销毁接口：会话不存在或已释放（可忽略）'],
    [409, 'SESSION_EXPIRED', '粘性会话到期（默认 10 分钟），已解除绑定，重试自动绑新 IP'],
    [409, 'POOL_EXHAUSTED', '池内无空闲代理可绑定，稍后再试'],
  ];

  return (
    <div>
      <div className="page-title">API 文档</div>
      <p className="page-desc">对外公开的接口文档。一套 API Key 同时管「代理提取」与「连接配置」，支持粘性会话与消耗式提取。Base URL：<code>{BASE}</code></p>

      <section className="card">
        <div className="card-hd"><h2 className="card-title">鉴权方式</h2></div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12, marginTop: 12 }}>
          <div className="note"><b>URL 查询参数</b><br /><code>?key=你的KEY</code><br /><span style={{ opacity: 0.7 }}>GET 请求最方便</span></div>
          <div className="note"><b>请求头</b><br /><code>X-API-Key: 你的KEY</code><br /><span style={{ opacity: 0.7 }}>POST 请求推荐</span></div>
          <div className="note"><b>未设置 Key</b><br />提取接口开放访问；<br />管理接口（config 等）返回 <code>403</code> 禁止</div>
        </div>
      </section>

      {endpoints.map((ep) => (
        <section className="card" key={ep.name}>
          <div className="card-hd">
            <h2 className="card-title">
              <span className={`method method-${ep.method.toLowerCase()}`}>{ep.method}</span> {ep.path}
            </h2>
            <span className="card-sub">{ep.name}</span>
          </div>
          <p style={{ margin: '10px 0 0', fontSize: 13, color: '#475569' }}>{ep.desc}</p>
          <div className="section-label" style={{ marginTop: 14 }}>参数</div>
          <T>
            <thead><tr><th style={{ width: 220 }}>参数</th><th style={{ width: 110 }}>必填</th><th>说明</th></tr></thead>
            <tbody>
              {ep.params.map(([p, req, d]) => (
                <tr key={p}>
                  <td><code>{p}</code></td>
                  <td>{req}</td>
                  <td>{d}</td>
                </tr>
              ))}
            </tbody>
          </T>
        </section>
      ))}

      <section className="card">
        <div className="card-hd"><h2 className="card-title">curl 示例</h2></div>
        <div className="codeblock" style={{ marginTop: 12 }}>
          <div><span className="cm"># 提取共享出口（非粘性环境）</span></div>
          <div>curl "<span className="hl">{BASE}/api/v1/proxy?key=你的KEY</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 注册流程：粘性会话（同 session 同 IP）</span></div>
          <div>curl "<span className="hl">{BASE}/api/v1/proxy?key=你的KEY&amp;session=user_001</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 注册成功：consume=1 烧掉该 IP，池子自动补新</span></div>
          <div>curl "<span className="hl">{BASE}/api/v1/proxy?key=你的KEY&amp;session=user_001&amp;consume=1</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 纯文本输出</span></div>
          <div>curl "<span className="hl">{BASE}/api/v1/proxy?key=你的KEY&amp;format=txt</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 注册失败：销毁该 IP</span></div>
          <div>curl "<span className="hl">{BASE}/api/v1/proxy/destroy?key=你的KEY&amp;session=user_001</span>"</div>
          <div style={{ marginTop: 10 }}><span className="cm"># 查看当前连接配置</span></div>
          <div>curl "<span className="hl">{BASE}/api/v1/config?key=你的KEY</span>"</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 改入口：混合模式 + 端口 + 账号密码</span></div>
          <div>curl -X POST "<span className="hl">{BASE}/api/v1/config?key=你的KEY</span>" -H "Content-Type: application/json" -d {'{"entry_mode":"mixed","entry_port":"7899","entry_username":"user1","entry_password":"pass1"}'}</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 切换直连场景</span></div>
          <div>curl -X POST "<span className="hl">{BASE}/api/v1/config?key=你的KEY</span>" -H "Content-Type: application/json" -d {'{"scenario":"A","switch":"1"}'}</div>
          <div style={{ marginTop: 6 }}><span className="cm"># 保存并应用场景 E（API 提取）</span></div>
          <div>curl -X POST "<span className="hl">{BASE}/api/v1/config?key=你的KEY</span>" -H "Content-Type: application/json" -d {'{"scenario":"E","api_url":"https://api.example.com?num=10","api_num":"1"}'}</div>
        </div>

        <div className="section-label">提取响应示例（JSON）</div>
        <div className="codeblock">
          <pre style={{ margin: 0 }}>{`{
  "ok": true,
  "session": "user_001",
  "proxy": {
    "proxy": "socks5://user:pass@192.6.121.16:7890",
    "ip": "192.6.121.16",
    "port": 7890
  },
  "sticky": { "bound": true, "expires_in": 599 }
}`}</pre>
        </div>
      </section>

      <section className="card">
        <div className="card-hd"><h2 className="card-title">错误码</h2></div>
        <T>
          <thead><tr><th style={{ width: 90 }}>HTTP</th><th style={{ width: 220 }}>code</th><th>说明</th></tr></thead>
          <tbody>
            {errors.map(([s, c, d]) => (
              <tr key={c as string}>
                <td><span className="badge">{s}</span></td>
                <td><code>{c}</code></td>
                <td>{d}</td>
              </tr>
            ))}
          </tbody>
        </T>
      </section>
    </div>
  );
}