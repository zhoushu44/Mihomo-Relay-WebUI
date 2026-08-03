#!/usr/bin/env python3
"""mihomo 代理管理 Web UI"""
import os, json, subprocess, yaml, time, threading, secrets
from functools import wraps
from urllib.parse import quote
try:
    import docker
except ImportError:
    docker = None
from flask import Flask, request, render_template_string, jsonify, session, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
PASSWORD = os.environ.get('UI_PASSWORD', 'mihomo123')
CONFIG_PATH = '/tmp/mihomo_config.yaml'
PROVIDERS_PATH = '/tmp/mihomo_providers.yaml'
REFRESH_SCRIPT = '/tmp/cliproxy_refresh.sh'
SETTINGS_PATH = '/tmp/terminal_settings.json'
MIHOMO_HOST = os.environ.get('MIHOMO_HOST', '127.0.0.1')
dk = None


def get_docker_client():
    global dk
    if docker is None:
        raise RuntimeError('未安装 Docker SDK for Python')
    if dk is None:
        dk = docker.from_env()
    return dk


def load_settings():
    defaults = {
        'socks': {'enabled': True, 'port': 7890, 'username': '', 'password': ''},
        'http': {'enabled': True, 'port': 7891, 'username': '', 'password': ''},
        'api_key': os.environ.get('API_KEY', '') or secrets.token_urlsafe(24),
        'scenario': 'A',
        'saved_scenarios': {},
    }
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            saved = json.load(f)
        for kind in ('socks', 'http'):
            defaults[kind].update(saved.get(kind, {}))
        defaults['api_key'] = saved.get('api_key') or defaults['api_key']
        defaults['scenario'] = saved.get('scenario', 'A')
        defaults['saved_scenarios'] = saved.get('saved_scenarios', {})
    except (OSError, ValueError, TypeError):
        save_settings(defaults)
    return defaults


def save_settings(settings):
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


def listener_config(settings):
    listeners = []
    for kind, listener_type in (('socks', 'socks'), ('http', 'http')):
        item = settings[kind]
        if not item['enabled']:
            continue
        listener = {
            'name': f'{kind}-terminal', 'type': listener_type,
            'listen': '0.0.0.0', 'port': int(item['port']),
        }
        if item['username']:
            listener['users'] = [{'username': item['username'], 'password': item['password']}]
        listeners.append(listener)
    return listeners


def attach_listeners(config_str, settings=None):
    cfg = yaml.safe_load(config_str) or {}
    cfg.pop('mixed-port', None)
    cfg.pop('socks-port', None)
    cfg.pop('port', None)
    cfg['listeners'] = listener_config(settings or load_settings())
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


def listener_proxy_url(kind, settings=None, host=None):
    item = (settings or load_settings())[kind]
    host = host or MIHOMO_HOST
    auth = ''
    if item['username']:
        auth = f"{quote(item['username'], safe='')}:{quote(item['password'], safe='')}@"
    scheme = 'socks5h' if kind == 'socks' else 'http'
    return f"{scheme}://{auth}{host}:{item['port']}"


def validate_terminal_settings(settings):
    enabled_ports = []
    for kind, label in (('socks', 'SOCKS5'), ('http', 'HTTP')):
        item = settings[kind]
        if bool(item['username']) != bool(item['password']):
            return f'{label} 用户名和密码必须成对填写'
        try:
            item['port'] = int(item['port'])
        except (TypeError, ValueError):
            return f'{label} 端口必须是整数'
        if item['enabled']:
            if not 1 <= item['port'] <= 65535:
                return f'{label} 端口必须在 1..65535'
            enabled_ports.append(item['port'])
    if len(enabled_ports) != len(set(enabled_ports)) or 7892 in enabled_ports:
        return '已启用入口端口不能互相冲突，也不能占用 API 控制端口 7892'
    if not settings['api_key']:
        return 'API Key 不能为空'
    return None

HTML = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mihomo Relay WebUI - Mihomo 代理中转管理终端</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;max-width:820px;margin:0 auto;padding:16px;background:#f5f5f5}
h1{font-size:1.4em}
.card{background:#fff;border-radius:8px;padding:16px;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.card h2{margin:0 0 10px;font-size:1.1em;border-left:3px solid #4CAF50;padding-left:8px;cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.card h2:hover{color:#4CAF50}
.card h2::after{content:'▼';font-size:.8em;transition:transform .3s}
.card.collapsed h2::after{transform:rotate(-90deg)}
.card.collapsed .content{display:none}
.content{margin-top:10px}
label{display:block;font-size:.85em;color:#666;margin:6px 0 2px}
input,select,textarea{width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px}
textarea{font-family:monospace}
button{padding:8px 18px;background:#4CAF50;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:14px;margin-top:8px}
button:hover{background:#43a047}
.btn-red{background:#f44336}.btn-red:hover{background:#d32f2f}
.btn-blue{background:#2196f3}.btn-blue:hover{background:#1976d2}
.row{display:flex;gap:10px}.row>div{flex:1}
.status{padding:8px 12px;border-radius:4px;margin:8px 0;font-size:14px}
.ok{background:#c8e6c9;color:#1b5e20}.err{background:#ffcdd2;color:#b71c1c}
pre{background:#f4f4f4;padding:10px;border-radius:4px;overflow-x:auto;font-size:12px;max-height:400px;overflow-y:auto}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.note{font-size:.8em;color:#888;margin:6px 0;padding:8px;background:#fff3cd;border-left:3px solid #ffc107}
.warning{background:#ffebee;color:#c62828}
.speed-result{margin:10px 0;padding:10px;background:#e8f5e9;border-radius:4px}
.speed-bar{height:8px;background:#e0e0e0;border-radius:4px;overflow:hidden;margin:5px 0}
.speed-fill{height:100%;background:linear-gradient(90deg,#4CAF50,#8BC34A);transition:width .3s}
.terminal-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.terminal-card{border:1px solid #ddd;border-radius:6px;padding:10px;min-width:0}
.terminal-card h3{margin:0 0 8px;font-size:1em}.inline-check{display:flex;align-items:center;gap:6px}.inline-check input{width:auto}
.secret-wrap{display:flex;gap:5px}.secret-wrap button{margin:0;padding:6px 10px}.mini-actions{display:flex;flex-wrap:wrap;gap:5px}.mini-actions button{padding:6px 10px}
.copy-value{font:12px monospace;overflow-wrap:anywhere;color:#555}.toast{display:none;position:fixed;right:18px;bottom:18px;background:#263238;color:white;padding:10px 14px;border-radius:5px;z-index:10}
@media(max-width:700px){.terminal-grid,.grid2{grid-template-columns:1fr}.row{flex-direction:column}}
</style>
</head>
<body>
<h1>Mihomo Relay WebUI</h1>
<p style="margin-top:-8px;color:#666">Mihomo 代理中转管理终端</p>

{% if not authed %}
<div class="card">
<h2>登录</h2>
<form method="post" action="/login">
<input type="password" name="pwd" placeholder="管理密码">
<button type="submit">进入</button>
</form>
</div>
{% else %}

<div class="card">
<h2>当前状态</h2>
<div class="content">
<div class="status {{ 'ok' if status.alive else 'err' }}">
  代理: <b>{{ '正常' if status.alive else '不可用' }}</b>
  {% if status.ip %}| 出口 IP: <b>{{ status.ip }}</b>{% endif %}
  {% if status.country %}({{ status.country }}){% endif %}
  {% if status.mode %}| 模式: {{ status.mode }}{% endif %}
</div>
<div class="row">
<form method="post" action="/action" style="flex:1"><input type="hidden" name="act" value="test"><button type="submit" class="btn-blue">测试代理</button></form>
<form method="post" action="/action" style="flex:1"><input type="hidden" name="act" value="speed"><button type="submit" class="btn-blue">测速</button></form>
<form method="post" action="/action" style="flex:1"><input type="hidden" name="act" value="restart"><button type="submit">重启 mihomo</button></form>
</div>
{% if speed_result %}
<div class="speed-result">
<div><b>测速结果</b>: {{ speed_result.speed }} Mbps (延迟: {{ speed_result.latency }} ms)</div>
<div class="speed-bar"><div class="speed-fill" style="width:{{ speed_result.bar_width }}%"></div></div>
<small>{{ speed_result.time }} 前</small>
</div>
{% endif %}
</div>
</div>

<div class="card">
<h2>对外连接</h2>
<div class="content">
<form method="post" action="/terminal-settings" id="terminalForm">
<div class="terminal-grid">
{% for kind, title, scheme in [('socks','SOCKS5','socks5'),('http','HTTP','http')] %}
<div class="terminal-card">
<h3>{{ title }}</h3>
<label class="inline-check"><input type="checkbox" name="{{ kind }}_enabled" {% if settings[kind].enabled %}checked{% endif %}> 启用入口</label>
<label>监听端口</label><input type="number" name="{{ kind }}_port" min="1" max="65535" value="{{ settings[kind].port }}">
<label>用户名</label><input type="text" name="{{ kind }}_username" value="{{ settings[kind].username }}" autocomplete="off">
<label>密码（留空表示保留已保存值）</label>
<div class="secret-wrap"><input type="password" name="{{ kind }}_password" placeholder="{% if settings[kind].password %}已保存{% else %}未设置{% endif %}" autocomplete="new-password"><button type="button" class="btn-blue" onclick="toggleSecret(this)">显示</button></div>
<p class="copy-value">{{ scheme }}://{{ public_host }}:{{ settings[kind].port }}</p>
<div class="mini-actions"><button type="button" class="btn-blue" onclick="copyConnection('{{ kind }}')">复制链接</button><button type="submit" name="test_kind" value="{{ kind }}" class="btn-blue">保存并测试</button></div>
</div>
{% endfor %}
<div class="terminal-card">
<h3>直连入口</h3>
<div class="note">直连模式下，入口仍使用 SOCKS5 / HTTP，但不经过任何上游代理，直接使用服务器出口 IP。</div>
<p class="copy-value">SOCKS5：socks5://{{ public_host }}:{{ settings.socks.port }}</p>
<p class="copy-value">HTTP：http://{{ public_host }}:{{ settings.http.port }}</p>
<button type="submit" name="terminal_action" value="direct" class="btn-blue">切换为直连</button>
<div class="mini-actions"><button type="button" class="btn-blue" onclick="copyConnection('socks')">复制 SOCKS5</button><button type="button" class="btn-blue" onclick="copyConnection('http')">复制 HTTP</button></div>
</div>
<div class="terminal-card">
<h3>API 控制接口</h3>
<label>监听地址</label><p class="copy-value">http://{{ public_host }}:7892</p>
<label>API Key</label><div class="secret-wrap"><input type="password" name="api_key" value="" placeholder="已保存，留空保留" autocomplete="new-password"><button type="button" class="btn-blue" onclick="toggleSecret(this)">显示</button></div>
{% for endpoint in ['connections','status','rotate'] %}<p class="copy-value">/api/{{ endpoint }}</p><button type="button" class="btn-blue" onclick="copyApi('{{ endpoint }}')">复制 {{ endpoint }}</button>{% endfor %}
</div>
</div>
<button type="submit">保存对外连接设置</button>
</form>
</div>
</div>

<div class="card">
<h2>场景 A：直连（无上游代理）{% if settings.scenario == 'A' %} <small style="color:#4CAF50">● 当前</small>{% endif %}</h2>
<div class="content">
<div class="note">
<b>说明</b>: mihomo 直接用服务器 IP 出口，不经过任何上游代理。<br>
<b>白名单</b>: 不需要
</div>
<form method="post" action="/apply">
<input type="hidden" name="scenario" value="A">
<button type="submit">保存应用</button>
</form>
{% if settings.saved_scenarios.get('A') %}
<form method="post" action="/apply" style="margin-top:8px"><input type="hidden" name="scenario" value="A"><input type="hidden" name="switch" value="1"><button type="submit" class="btn-blue">切换</button></form>
{% endif %}
</div>
</div>

<div class="card">
<h2>场景 B/C/D：挂代理（SOCKS5 / HTTP）{% if settings.scenario == 'proxy' %} <small style="color:#4CAF50">● 当前</small>{% endif %}</h2>
<div class="content">
<div class="note">
<b>格式</b>: 每行一个代理 <code>ip:port</code> 或 <code>ip:port:user:pass</code><br>
<b>白名单</b>: 不需要（用账号密码认证）<br>
<b>Clash 链接</b>: 支持订阅链接转换后的代理列表
</div>
<form method="post" action="/apply">
<input type="hidden" name="scenario" value="proxy">
<div class="grid2">
<div><label>代理类型</label><select name="proxy_type"><option value="socks5" {% if settings.saved_scenarios.get('proxy',{}).get('proxy_type')=='socks5' %}selected{% endif %}>SOCKS5</option><option value="http" {% if settings.saved_scenarios.get('proxy',{}).get('proxy_type')=='http' %}selected{% endif %}>HTTP</option></select></div>
<div><label>轮换策略</label><select name="rotate"><option value="yes" {% if settings.saved_scenarios.get('proxy',{}).get('rotate')!='no' %}selected{% endif %}>轮换（round-robin）</option><option value="no" {% if settings.saved_scenarios.get('proxy',{}).get('rotate')=='no' %}selected{% endif %}>只用第一个</option></select></div>
</div>
<label>代理列表（每行一个）</label>
<textarea name="proxies" rows="4" placeholder="1.2.3.4:1080&#10;5.6.7.8:8080&#10;9.10.11.12:3128:user:pass">{{ settings.saved_scenarios.get('proxy',{}).get('proxies','') }}</textarea>
<label>统一用户名（可选，留空则用每行的）</label>
<input type="text" name="username" placeholder="留空" value="{{ settings.saved_scenarios.get('proxy',{}).get('username','') }}">
<label>统一密码（可选）</label>
<input type="text" name="password" placeholder="留空" value="{{ settings.saved_scenarios.get('proxy',{}).get('password','') }}">
<button type="submit">保存应用</button>
</form>
{% if settings.saved_scenarios.get('proxy') %}
<form method="post" action="/apply" style="margin-top:8px"><input type="hidden" name="scenario" value="proxy"><input type="hidden" name="switch" value="1"><button type="submit" class="btn-blue">切换</button></form>
{% endif %}
</div>
</div>

<div class="card">
<h2>场景 E：API 提取（用到才提取）{% if settings.scenario == 'E' %} <small style="color:#4CAF50">● 当前</small>{% endif %}</h2>
<div class="content">
<div class="note warning">
<b>前提</b>: 服务器 IP 必须在上游代理平台加白<br>
<b>机制</b>: 每 2 分钟检测一次，代理过期才提取，不浪费额度
</div>
<form method="post" action="/apply">
<input type="hidden" name="scenario" value="E">
<label>API URL</label>
<input type="text" name="api_url" placeholder="https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt" value="{{ settings.saved_scenarios.get('E',{}).get('api_url', saved_api) }}">
<label>提取数量</label>
<input type="number" name="api_num" value="{{ settings.saved_scenarios.get('E',{}).get('api_num','1') }}" min="1" max="50">
<button type="submit">保存应用</button>
</form>
{% if settings.saved_scenarios.get('E') %}
<form method="post" action="/apply" style="margin-top:8px"><input type="hidden" name="scenario" value="E"><input type="hidden" name="switch" value="1"><button type="submit" class="btn-blue">切换</button></form>
{% endif %}
<form method="post" action="/action"><input type="hidden" name="act" value="refresh"><button type="submit" class="btn-blue">立即刷新代理</button></form>
</div>
</div>

<div class="card">
<h2>场景 F：Clash 订阅链接{% if settings.scenario == 'F' %} <small style="color:#4CAF50">● 当前</small>{% endif %}</h2>
<div class="content">
<div class="note">
<b>用法</b>: 填入 Clash 订阅链接，自动解析为代理列表<br>
<b>支持</b>: Clash、Clash.Meta 订阅格式<br>
<b>白名单</b>: 不需要（取决于订阅内容）
</div>
<form method="post" action="/apply">
<input type="hidden" name="scenario" value="F">
<label>Clash 订阅 URL</label>
<input type="text" name="clash_url" placeholder="https://example.com/sub?token=xxx" value="{{ settings.saved_scenarios.get('F',{}).get('clash_url','') }}">
<button type="submit">保存应用</button>
</form>
{% if settings.saved_scenarios.get('F') %}
<form method="post" action="/apply" style="margin-top:8px"><input type="hidden" name="scenario" value="F"><input type="hidden" name="switch" value="1"><button type="submit" class="btn-blue">切换</button></form>
{% endif %}
</div>
</div>

{% if message %}
<div class="status {{ 'ok' if success else 'err' }}">{{ message }}</div>
{% endif %}

{% if current_config %}
<div class="card">
<h2>当前配置文件</h2>
<div class="content"><pre>{{ current_config }}</pre></div>
</div>
{% endif %}

<script>
document.querySelectorAll('.card h2').forEach(h2 => {
    h2.addEventListener('click', () => {
        h2.parentElement.classList.toggle('collapsed');
    });
});
const terminalSettings = {{ settings_public|tojson }};
function toggleSecret(btn) {
    const input = btn.previousElementSibling;
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.textContent = input.type === 'password' ? '显示' : '隐藏';
}
function notify(text) {
    const toast = document.getElementById('toast'); toast.textContent = text; toast.style.display = 'block';
    setTimeout(() => toast.style.display = 'none', 1800);
}
function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(() => notify('复制成功'));
    const area = document.createElement('textarea'); area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select(); const ok = document.execCommand('copy'); area.remove(); notify(ok ? '复制成功' : '复制失败');
}
function copyConnection(kind) {
    const form = document.getElementById('terminalForm');
    const user = form.elements[kind + '_username'].value;
    const entered = form.elements[kind + '_password'].value;
    const password = entered || terminalSettings[kind].password;
    const port = form.elements[kind + '_port'].value;
    const auth = user && password ? encodeURIComponent(user) + ':' + encodeURIComponent(password) + '@' : '';
    copyText((kind === 'socks' ? 'socks5' : 'http') + '://' + auth + {{ public_host|tojson }} + ':' + port);
}
function copyApi(endpoint) {
    copyText('http://' + {{ public_host|tojson }} + ':7892/api/' + endpoint + '?key=' + encodeURIComponent(terminalSettings.api_key));
}
</script>
<div id="toast" class="toast"></div>
{% endif %}
</body>
</html>'''


def render_page(**context):
    settings = load_settings()
    public_host = request.host.split(':', 1)[0]
    settings_public = json.loads(json.dumps(settings))
    config_text = context.get('current_config')
    if config_text:
        try:
            visible_cfg = yaml.safe_load(config_text) or {}
            for listener in visible_cfg.get('listeners', []):
                for user in listener.get('users', []):
                    if user.get('password'):
                        user['password'] = '********'
            context['current_config'] = yaml.safe_dump(visible_cfg, allow_unicode=True, sort_keys=False)
        except yaml.YAMLError:
            context['current_config'] = '配置已生成（为避免泄露敏感信息，无法直接展示）'
    context.update(settings=settings, settings_public=settings_public, public_host=public_host)
    return render_template_string(HTML, **context)


def available_listener(settings=None):
    settings = settings or load_settings()
    for kind in ('http', 'socks'):
        if settings[kind]['enabled']:
            return kind
    return None


def test_proxy(kind=None):
    settings = load_settings()
    kind = kind or available_listener(settings)
    if not kind:
        return {'alive': False, 'ip': '', 'country': ''}
    try:
        r = subprocess.run(
            ['curl', '-x', listener_proxy_url(kind, settings), '-sS', '--connect-timeout', '5', '--max-time', '10',
             'http://ip-api.com/json'],
            capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout)
        if data.get('status') == 'success':
            return {'alive': True, 'ip': data.get('query', '?'), 'country': data.get('country', '?')}
    except:
        pass
    return {'alive': False, 'ip': '', 'country': ''}


def get_mode():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
            if cfg and 'rules' in cfg:
                rule = str(cfg['rules'][0])
                if 'DIRECT' in rule: return '直连'
                if 'rotate' in rule: return '轮换'
                return '单代理'
    except:
        pass
    return '?'


def gen_direct_config():
    return """mixed-port: 7890
allow-lan: true
bind-address: "*"
mode: rule
log-level: info
ipv6: false
rules:
  - MATCH,DIRECT
"""


def gen_proxy_config(proxy_type, proxies_lines, username, password, rotate):
    proxies = []
    for i, line in enumerate(proxies_lines, 1):
        line = line.strip()
        if not line:
            continue
        parts = line.split(':')
        server = parts[0]
        port = int(parts[1])
        u = username or (parts[2] if len(parts) > 2 else None)
        p = password or (parts[3] if len(parts) > 3 else None)
        node = {'name': f'p{i}', 'type': proxy_type, 'server': server, 'port': port}
        if u: node['username'] = u
        if p: node['password'] = p
        proxies.append(node)

    if not proxies:
        return None, '代理列表为空'

    lines = [f"""mixed-port: 7890
allow-lan: true
bind-address: "*"
mode: rule
log-level: info
ipv6: false

proxies:"""]
    for p in proxies:
        items = [f"{{name: {p['name']}, type: {p['type']}, server: {p['server']}, port: {p['port']}"]
        if 'username' in p:
            items.append(f", username: \"{p['username']}\", password: \"{p['password']}\"")
        items.append("}")
        lines.append(f"  - {''.join(items)}")

    if rotate == 'yes' and len(proxies) > 1:
        names = ', '.join(p['name'] for p in proxies)
        lines.append(f"""
proxy-groups:
  - name: rotate
    type: load-balance
    strategy: round-robin
    proxies: [{names}]
    url: http://ip-api.com/json
    interval: 300

rules:
  - MATCH,rotate""")
    else:
        lines.append(f"\nrules:\n  - MATCH,{proxies[0]['name']}")

    return '\n'.join(lines) + '\n', None


def gen_api_config(api_url, num):
    # 将用户填的 num 替换/追加到 API URL，真正控制提取数量
    if 'num=' in api_url:
        import re
        api_url = re.sub(r'num=\d+', f'num={num}', api_url)
    elif '?' in api_url:
        api_url = api_url + f'&num={num}'
    else:
        api_url = api_url + f'?num={num}'
    local_proxy = listener_proxy_url(available_listener() or 'http')
    script = f"""#!/bin/bash
API_URL={json.dumps(api_url)}
OUTPUT="{PROVIDERS_PATH}"
TEST_URL="http://ip-api.com/json"
LOG="/tmp/cliproxy_refresh.log"
ALIVE=$(curl -x {json.dumps(local_proxy)} -sS --connect-timeout 5 --max-time 10 "$TEST_URL" 2>/dev/null | grep -c '"status":"success"')
if [ "$ALIVE" = "1" ]; then exit 0; fi
PROXIES=$(curl -sS --connect-timeout 10 --max-time 15 "$API_URL" 2>/dev/null)
if [ -z "$PROXIES" ]; then exit 1; fi
echo "proxies:" > "$OUTPUT"
i=1
while IFS= read -r line; do
    [ -z "$line" ] && continue
    host=$(echo "$line" | cut -d: -f1)
    port=$(echo "$line" | cut -d: -f2)
    echo "  - {{name: api$i, type: http, server: $host, port: $port}}" >> "$OUTPUT"
    i=$((i+1))
done <<< "$PROXIES"
docker restart mihomo >/dev/null 2>&1
echo "$(date): Extracted $((i-1)) proxies" >> "$LOG"
"""
    with open(REFRESH_SCRIPT, 'w') as f:
        f.write(script)
    os.chmod(REFRESH_SCRIPT, 0o755)
    subprocess.run(['bash', REFRESH_SCRIPT], timeout=30)

    config = """mixed-port: 7890
allow-lan: true
bind-address: "*"
mode: rule
log-level: info
ipv6: false

proxy-providers:
  cliproxy:
    type: file
    path: /root/.config/mihomo/providers.yaml
    health-check:
      enable: true
      url: http://ip-api.com/json
      interval: 60

proxy-groups:
  - name: rotate
    type: load-balance
    strategy: round-robin
    use: [cliproxy]
    url: http://ip-api.com/json
    interval: 60

rules:
  - MATCH,rotate
"""
    subprocess.run(
        'bash -c \'(crontab -l 2>/dev/null | grep -v cliproxy_refresh; echo "*/2 * * * * /bin/bash /tmp/cliproxy_refresh.sh") | crontab -\'',
        shell=True, capture_output=True)
    return config


def gen_subscription_config(subscription_url):
    """让 mihomo 直接拉取并解析远程订阅，不把 VLESS/Hysteria2 当成 HTTP 代理。"""
    return f'''mode: rule
log-level: info
ipv6: false

proxy-providers:
  subscription:
    type: http
    url: {json.dumps(subscription_url)}
    path: /root/.config/mihomo/subscription.yaml
    interval: 600
    health-check:
      enable: true
      url: http://ip-api.com/json
      interval: 60

proxy-groups:
  - name: rotate
    type: load-balance
    strategy: round-robin
    use: [subscription]
    url: http://ip-api.com/json
    interval: 60

rules:
  - MATCH,rotate
'''


def parse_clash_yaml(yaml_content):
    """解析 Clash 订阅 YAML 为代理列表"""
    try:
        cfg = yaml.safe_load(yaml_content)
        proxies = cfg.get('proxies', [])
        lines = []
        for p in proxies:
            if p.get('type') in ('http', 'socks5'):
                u = p.get('username', '')
                pw = p.get('password', '')
                auth = f":{u}:{pw}" if u else ''
                lines.append(f"{p['server']}:{p['port']}{auth}")
        return lines, None
    except Exception as e:
        return [], str(e)


def deploy_mihomo(config_str, scenario_e=False, settings=None):
    settings = settings or load_settings()
    config_str = attach_listeners(config_str, settings)
    with open(CONFIG_PATH, 'w') as f:
        f.write(config_str)
    try:
        client = get_docker_client()
        client.containers.get('mihomo').remove(force=True)
    except Exception:
        pass
    volumes = {CONFIG_PATH: {'bind': '/config.yaml', 'mode': 'ro'}}
    if scenario_e:
        volumes[PROVIDERS_PATH] = {'bind': '/root/.config/mihomo/providers.yaml', 'mode': 'ro'}
    ports = {f"{item['port']}/tcp": item['port'] for item in (settings['socks'], settings['http']) if item['enabled']}
    try:
        get_docker_client().containers.run(
            'metacubex/mihomo:latest', ['-f', '/config.yaml'], name='mihomo', detach=True,
            restart_policy={'Name': 'always'}, ports=ports, volumes=volumes)
    except Exception as e:
        return False, str(e)
    return True, None


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('authed'):
            return redirect(url_for('index'))
        return fn(*args, **kwargs)
    return wrapped


@app.route('/')
def index():
    authed = bool(session.get('authed'))
    status = test_proxy() if authed else {'alive': False}
    if authed:
        status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg = f.read()
    except:
        cfg = ''
    return render_page(authed=authed, status=status, current_config=cfg, message='', success=True,
                       saved_api='https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt',
                       speed_result=None)


@app.route('/login', methods=['POST'])
def login():
    pwd = request.form.get('pwd', '')
    if secrets.compare_digest(pwd, PASSWORD):
        session['authed'] = True
        return redirect(url_for('index'))
    return render_page(authed=False, status={'alive': False}, current_config='', message='密码错误',
                       success=False, saved_api='', speed_result=None)


def index_route(authed=False):
    status = test_proxy() if authed else {'alive': False}
    if authed:
        status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg = f.read()
    except:
        cfg = ''
    return render_page(authed=authed, status=status, current_config=cfg, message='', success=True,
                       saved_api='https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt',
                       speed_result=None)


@app.route('/apply', methods=['POST'])
@login_required
def apply():
    scenario = request.form.get('scenario')
    is_switch = request.form.get('switch') == '1'
    settings = load_settings()
    saved = settings.get('saved_scenarios', {})
    msg, ok = '', False

    # 切换模式：从已保存的参数加载
    if is_switch:
        params = saved.get(scenario, {})
        if not params and scenario != 'A':
            msg = '没有已保存的配置，请先保存应用'
        elif scenario == 'A':
            params = {'configured': True}
    else:
        # 保存模式：从表单读取并存储
        params = {}
        if scenario == 'proxy':
            params = {
                'proxy_type': request.form.get('proxy_type', 'socks5'),
                'proxies': request.form.get('proxies', '').strip(),
                'username': request.form.get('username', '').strip(),
                'password': request.form.get('password', '').strip(),
                'rotate': request.form.get('rotate', 'yes'),
            }
        elif scenario == 'E':
            params = {
                'api_url': request.form.get('api_url', '').strip(),
                'api_num': request.form.get('api_num', '1'),
            }
        elif scenario == 'F':
            params = {
                'clash_url': request.form.get('clash_url', '').strip(),
            }
        elif scenario == 'A':
            params = {'configured': True}
        saved[scenario] = params
        settings['saved_scenarios'] = saved

    if not msg:
        if scenario == 'A':
            cfg = gen_direct_config()
            ok, err = deploy_mihomo(cfg)
            msg = '已切换到直连模式' if ok else f'失败：{err}'
            subprocess.run('bash -c \'crontab -l 2>/dev/null | grep -v cliproxy_refresh | crontab -\'', shell=True, capture_output=True)

        elif scenario == 'proxy':
            ptype = params.get('proxy_type', 'socks5')
            proxies = params.get('proxies', '').strip().split('\n')
            username = params.get('username', '') or None
            password = params.get('password', '') or None
            rotate = params.get('rotate', 'yes')
            cfg, err = gen_proxy_config(ptype, proxies, username, password, rotate)
            if err:
                msg = err
            else:
                subprocess.run('bash -c \'crontab -l 2>/dev/null | grep -v cliproxy_refresh | crontab -\'', shell=True, capture_output=True)
                ok, err = deploy_mihomo(cfg)
                msg = f'已应用 {len(proxies)} 个 {ptype} 代理' + ('（轮换）' if rotate == 'yes' else '') if ok else f'失败：{err}'

        elif scenario == 'E':
            api_url = params.get('api_url', '').strip()
            num = params.get('api_num', '1')
            if not api_url:
                msg = 'API URL 不能为空'
            else:
                cfg = gen_api_config(api_url, num)
                ok, err = deploy_mihomo(cfg, scenario_e=True)
                msg = '已切换到 API 提取模式，代理已刷新' if ok else f'失败：{err}'

        elif scenario == 'F':
            clash_url = params.get('clash_url', '').strip()
            if not clash_url:
                msg = 'Clash 订阅 URL 不能为空'
            else:
                try:
                    import urllib.request, base64
                    r = urllib.request.urlopen(clash_url, timeout=15)
                    content = r.read().decode('utf-8', errors='replace')
                    proxies, err = parse_clash_yaml(content)
                    if err or not proxies:
                        compact = ''.join(content.split())
                        try:
                            decoded = base64.b64decode(compact + '=' * (-len(compact) % 4)).decode('utf-8', errors='replace')
                        except Exception:
                            decoded = ''
                        if any(decoded.strip().startswith(prefix) for prefix in ('vmess://', 'vless://', 'hysteria2://', 'ss://', 'trojan://')):
                            cfg = gen_subscription_config(clash_url)
                            subprocess.run('bash -c \'crontab -l 2>/dev/null | grep -v cliproxy_refresh | crontab -\'', shell=True, capture_output=True)
                            ok, err = deploy_mihomo(cfg, settings=settings)
                            msg = '已识别为 Base64/VLESS/Hysteria2 订阅，并交给 mihomo 解析应用' if ok else f'失败：{err}'
                            proxies = []
                        elif err:
                            msg = f'解析失败：{err}'
                        else:
                            msg = '未找到 HTTP/SOCKS5 代理'
                    if proxies:
                        cfg, err = gen_proxy_config('http', proxies, None, None, 'yes')
                        if err:
                            msg = err
                        else:
                            subprocess.run('bash -c \'crontab -l 2>/dev/null | grep -v cliproxy_refresh | crontab -\'', shell=True, capture_output=True)
                            ok, err = deploy_mihomo(cfg)
                            msg = f'已解析 {len(proxies)} 个代理并应用' if ok else f'失败：{err}'
                except Exception as e:
                    msg = f'下载失败：{e}'

    time.sleep(2)
    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except:
        cfg_text = ''
    if ok:
        settings['scenario'] = scenario
        save_settings(settings)
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api=params.get('api_url', ''), speed_result=None)


@app.route('/action', methods=['POST'])
@login_required
def action():
    act = request.form.get('act')
    msg, ok = '', True
    speed_result = None

    if act == 'test':
        status = test_proxy()
        status['mode'] = get_mode()
        if status['alive']:
            msg = f'代理正常，出口 IP: {status["ip"]} ({status["country"]})'
        else:
            msg = '代理不可用'
            ok = False
        try:
            with open(CONFIG_PATH) as f:
                cfg_text = f.read()
        except:
            cfg_text = ''
        return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                           saved_api='https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt',
                           speed_result=None)

    elif act == 'restart':
        try:
            get_docker_client().containers.get('mihomo').restart()
            time.sleep(2)
            msg = 'mihomo 已重启'
        except Exception as e:
            msg = f'重启失败：{e}'
            ok = False

    elif act == 'refresh':
        r = subprocess.run(['bash', REFRESH_SCRIPT], capture_output=True, text=True, timeout=30)
        time.sleep(2)
        msg = '代理已刷新' if r.returncode == 0 else '刷新失败（代理可能还活着）'

    elif act == 'speed':
        start = time.time()
        try:
            r = subprocess.run(
                ['curl', '-x', listener_proxy_url(available_listener() or 'http'), '-sS', '-o', '/dev/null',
                 '-w', '%{http_code} %{size_download} %{speed_download} %{time_total}', '--max-time', '30',
                 'https://speed.cloudflare.com/__down?bytes=1048576'],
                capture_output=True, text=True, timeout=35)
            parts = r.stdout.strip().split()
            if r.returncode == 0 and len(parts) >= 4 and parts[0] == '200' and float(parts[1]) > 0 and float(parts[2]) > 0:
                speed_bps = float(parts[2])
                latency = float(parts[3]) * 1000
                speed_mbps = (speed_bps * 8) / 1_000_000
                bar = min(100, max(1, int(speed_mbps / 50 * 100)))
                speed_result = {'speed': round(speed_mbps, 2), 'latency': round(latency, 1), 'time': '刚刚'}
                speed_result['bar_width'] = bar
                msg = f'测速完成：{speed_result["speed"]} Mbps'
            else:
                msg = '测速失败'
                ok = False
        except Exception as e:
            msg = f'测速失败：{e}'
            ok = False

    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api='https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt',
                       speed_result=speed_result)


@app.route('/terminal-settings', methods=['POST'])
@login_required
def terminal_settings():
    old = load_settings()
    terminal_action = request.form.get('terminal_action')
    settings = {'scenario': 'A' if terminal_action == 'direct' else old['scenario'], 'api_key': request.form.get('api_key', '').strip() or old['api_key']}
    for kind in ('socks', 'http'):
        entered_password = request.form.get(f'{kind}_password', '')
        settings[kind] = {
            'enabled': request.form.get(f'{kind}_enabled') == 'on',
            'port': request.form.get(f'{kind}_port', ''),
            'username': request.form.get(f'{kind}_username', '').strip(),
            'password': entered_password if entered_password else old[kind]['password'],
        }
        if not settings[kind]['username']:
            settings[kind]['password'] = ''
    error = validate_terminal_settings(settings)
    ok, msg = False, error or ''
    if not error:
        if terminal_action == 'direct':
            current = gen_direct_config()
        else:
            try:
                with open(CONFIG_PATH) as f:
                    current = f.read()
            except OSError:
                current = gen_direct_config()
        ok, deploy_error = deploy_mihomo(current, scenario_e=settings['scenario'] == 'E', settings=settings)
        if ok:
            save_settings(settings)
            test_kind = request.form.get('test_kind')
            if test_kind:
                result = test_proxy(test_kind)
                ok = result['alive']
                msg = f'{test_kind.upper()} 入口测试通过' if ok else f'{test_kind.upper()} 入口测试失败'
            else:
                msg = '对外连接设置已保存并应用'
        else:
            msg = f'应用失败：{deploy_error}'
    status = test_proxy() if ok else {'alive': False, 'ip': '', 'country': ''}
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except OSError:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api='', speed_result=None)


def api_authorized():
    settings = load_settings()
    supplied = request.args.get('key', '') or request.headers.get('X-API-Key', '')
    return bool(supplied) and secrets.compare_digest(supplied, settings['api_key'])


def api_auth_error():
    return jsonify({'ok': False, 'error': 'unauthorized'}), 401


@app.route('/api/connections')
def api_connections():
    if not api_authorized():
        return api_auth_error()
    settings = load_settings()
    host = request.host.split(':', 1)[0]
    result = {}
    for kind in ('socks', 'http'):
        item = settings[kind]
        url = listener_proxy_url(kind, settings, host).replace('socks5h://', 'socks5://')
        masked_url = url
        if item['password']:
            masked_url = url.replace(quote(item['password'], safe=''), '********', 1)
        result[kind] = {
            'enabled': item['enabled'], 'host': host, 'port': item['port'],
            'username': item['username'], 'password_set': bool(item['password']),
            'masked_url': masked_url, 'url': url,
        }
    return jsonify({'ok': True, 'connections': result})


@app.route('/api/status')
def api_status():
    if not api_authorized():
        return api_auth_error()
    status = test_proxy()
    status['mode'] = get_mode()
    return jsonify({'ok': True, 'status': status})


@app.route('/api/rotate', methods=['GET', 'POST'])
def api_rotate():
    if not api_authorized():
        return api_auth_error()
    settings = load_settings()
    try:
        if settings['scenario'] == 'E':
            result = subprocess.run(['bash', REFRESH_SCRIPT], capture_output=True, text=True, timeout=30)
            return jsonify({'ok': result.returncode == 0, 'action': 'refresh', 'message': '刷新完成' if result.returncode == 0 else '刷新失败'})
        get_docker_client().containers.get('mihomo').restart()
        return jsonify({'ok': True, 'action': 'restart', 'message': '非 API 提取模式，已重启 mihomo'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7892)
