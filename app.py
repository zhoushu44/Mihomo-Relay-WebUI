#!/usr/bin/env python3
"""mihomo 代理管理 Web UI（含商用级粘性会话）"""
import os, json, subprocess, yaml, time, threading, secrets, logging, base64, re, urllib.parse, urllib.request, socket
from functools import wraps
from urllib.parse import quote
try:
    import docker
except ImportError:
    docker = None
from flask import Flask, request, render_template_string, jsonify, session, redirect, url_for, send_from_directory

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('mihomo-web')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)


@app.after_request
def _no_cache(resp):
    """禁止浏览器缓存页面，确保每次操作都拿到最新版本（避免旧 JS/旧界面导致'点了没反应'）"""
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


PASSWORD = os.environ.get('UI_PASSWORD', 'mihomo123')
CONFIG_PATH = '/tmp/mihomo_config.yaml'
PROVIDERS_PATH = '/tmp/mihomo_providers.yaml'
REFRESH_SCRIPT = '/tmp/cliproxy_refresh.sh'
SETTINGS_PATH = '/tmp/terminal_settings.json'
MIHOMO_HOST = os.environ.get('MIHOMO_HOST', 'host.docker.internal')
dk = None

# ==================== 粘性会话全局状态 ====================
STICKY_LOCK = threading.Lock()
RELOAD_LOCK = threading.Lock()
STICKY_STATE = {
    'enabled': False,
    'test_url': 'http://ip-api.com/json',
    'test_enabled': True,
    'timeout': 600,
    'queue_timeout': 30,
    'port_start': 40001,
    'port_end': 40999,
    'sessions': {},          # task_id -> session
    'ports': {},             # port -> task_id
    'pool': {},              # proxy_key -> 代理信息（B/C/D）
    'rr': 0,                 # round-robin 指针
    'f_nodes': [],           # F 已解析节点
    'f_updated': 0,
    'f_mode': 'direct',      # F 模式：direct / poll
    'f_fail': {},            # F 节点名 -> 连续失败次数
    'e_test_nodes': [],      # E 场景测试节点（测速/测试/切换时自动提取，避免显示空）
    'e_extract_cd': 0,       # E 测试节点提取失败冷却截止时间戳
    'e_extract_cd_err': '',  # 冷却期直接返回的失败原因
}


def get_docker_client():
    global dk
    if docker is None:
        raise RuntimeError('未安装 Docker SDK for Python')
    if dk is None:
        dk = docker.from_env()
    return dk


# ==================== 配置持久化 ====================
def load_settings():
    defaults = {
        'socks': {'enabled': True, 'port': 7890, 'username': '', 'password': ''},
        'http': {'enabled': True, 'port': 7891, 'username': '', 'password': ''},
        'entry_mode': 'mixed',   # 对外入口模式：mixed(混合单端口) / socks / http / dual(双入口)
        'exit_mode': 'scenario',  # 对外出口：scenario(跟随场景) / direct(强制直连)
        'api_key': os.environ.get('API_KEY', '') or secrets.token_urlsafe(24),
        'scenario': 'A',
        'saved_scenarios': {},
        'sticky': {'enabled': False, 'test_url': 'http://ip-api.com/json',
                   'test_enabled': True, 'timeout': 600, 'queue_timeout': 30},
    }
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            saved = json.load(f)
        for kind in ('socks', 'http'):
            defaults[kind].update(saved.get(kind, {}))
        defaults['api_key'] = saved.get('api_key') or defaults['api_key']
        defaults['scenario'] = saved.get('scenario', 'A')
        defaults['saved_scenarios'] = saved.get('saved_scenarios', {})
        defaults['sticky'].update(saved.get('sticky', {}))
        defaults['entry_mode'] = saved.get('entry_mode', 'mixed')
        defaults['exit_mode'] = saved.get('exit_mode', 'scenario')
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


def _sync_sticky_from_settings(settings=None):
    """把持久化粘性配置同步到内存状态"""
    settings = settings or load_settings()
    st = settings.get('sticky', {})
    with STICKY_LOCK:
        STICKY_STATE['enabled'] = bool(st.get('enabled', False))
        STICKY_STATE['test_url'] = st.get('test_url') or 'http://ip-api.com/json'
        STICKY_STATE['test_enabled'] = bool(st.get('test_enabled', True))
        STICKY_STATE['timeout'] = int(st.get('timeout', 600))
        STICKY_STATE['queue_timeout'] = int(st.get('queue_timeout', 30))
        f_params = settings['saved_scenarios'].get('F', {})
        STICKY_STATE['f_mode'] = f_params.get('mode', 'direct')


_sync_sticky_from_settings()


# ==================== 基础工具 ====================
def listener_config(settings):
    """按入口模式生成对外 listener：
    mixed = 混合单端口（同一端口自动识别 SOCKS5/HTTP）
    socks = 仅 SOCKS5；http = 仅 HTTP；dual = SOCKS5 + HTTP 双入口"""
    listeners = []
    mode = settings.get('entry_mode', 'dual')
    def _mk(name, ltype, item):
        lis = {'name': name, 'type': ltype, 'listen': '0.0.0.0', 'port': int(item['port'])}
        if item.get('username'):
            lis['users'] = [{'username': item['username'], 'password': item['password']}]
        return lis
    if mode == 'mixed':
        item = settings['socks']
        if item['enabled']:
            listeners.append(_mk('mixed-terminal', 'mixed', item))
    elif mode == 'socks':
        item = settings['socks']
        if item['enabled']:
            listeners.append(_mk('socks-terminal', 'socks', item))
    elif mode == 'http':
        item = settings['http']
        if item['enabled']:
            listeners.append(_mk('http-terminal', 'http', item))
    else:  # dual 双入口
        for kind, ltype in (('socks', 'socks'), ('http', 'http')):
            item = settings[kind]
            if item['enabled']:
                listeners.append(_mk(f'{kind}-terminal', ltype, item))
    return listeners


def attach_listeners(config_str, settings=None):
    cfg = yaml.safe_load(config_str) or {}
    cfg.pop('mixed-port', None)
    cfg.pop('socks-port', None)
    cfg.pop('port', None)
    cfg['listeners'] = listener_config(settings or load_settings()) + _sticky_listeners()
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


def available_listener(settings=None):
    settings = settings or load_settings()
    for kind in ('http', 'socks'):
        if settings[kind]['enabled']:
            return kind
    return None


# ip-api 国家英文名 -> 中文（出口国家显示用）
COUNTRY_ZH = {
    'China': '中国', 'Hong Kong': '中国香港', 'Macao': '中国澳门', 'Taiwan': '中国台湾',
    'United States': '美国', 'Japan': '日本', 'Korea, Republic of': '韩国', 'South Korea': '韩国',
    'Singapore': '新加坡', 'Netherlands': '荷兰', 'Germany': '德国', 'France': '法国',
    'United Kingdom': '英国', 'Russia': '俄罗斯', 'Canada': '加拿大', 'Australia': '澳大利亚',
    'India': '印度', 'Malaysia': '马来西亚', 'Thailand': '泰国', 'Vietnam': '越南',
    'Indonesia': '印度尼西亚', 'Philippines': '菲律宾', 'Turkey': '土耳其', 'Brazil': '巴西',
    'Argentina': '阿根廷', 'Mexico': '墨西哥', 'Italy': '意大利', 'Spain': '西班牙',
    'Poland': '波兰', 'Sweden': '瑞典', 'Norway': '挪威', 'Finland': '芬兰',
    'Denmark': '丹麦', 'Belgium': '比利时', 'Switzerland': '瑞士', 'Austria': '奥地利',
    'Ireland': '爱尔兰', 'Portugal': '葡萄牙', 'Greece': '希腊', 'Israel': '以色列',
    'United Arab Emirates': '阿联酋', 'Saudi Arabia': '沙特阿拉伯', 'South Africa': '南非',
    'Egypt': '埃及', 'Nigeria': '尼日利亚', 'Ukraine': '乌克兰', 'Czechia': '捷克',
    'Romania': '罗马尼亚', 'Hungary': '匈牙利', 'Bulgaria': '保加利亚', 'New Zealand': '新西兰',
    'Iceland': '冰岛', 'Luxembourg': '卢森堡', 'Estonia': '爱沙尼亚', 'Latvia': '拉脱维亚',
    'Lithuania': '立陶宛', 'Slovakia': '斯洛伐克', 'Slovenia': '斯洛文尼亚', 'Croatia': '克罗地亚',
    'Serbia': '塞尔维亚', 'Kazakhstan': '哈萨克斯坦', 'Mongolia': '蒙古', 'Bangladesh': '孟加拉国',
    'Pakistan': '巴基斯坦', 'Sri Lanka': '斯里兰卡', 'Nepal': '尼泊尔', 'Iran': '伊朗',
    'Iraq': '伊拉克', 'Qatar': '卡塔尔', 'Kuwait': '科威特', 'Oman': '阿曼',
    'Jordan': '约旦', 'Lebanon': '黎巴嫩', 'Morocco': '摩洛哥', 'Algeria': '阿尔及利亚',
    'Tunisia': '突尼斯', 'Kenya': '肯尼亚', 'Colombia': '哥伦比亚', 'Chile': '智利',
    'Peru': '秘鲁', 'Venezuela': '委内瑞拉', 'Ecuador': '厄瓜多尔', 'Panama': '巴拿马',
    'Costa Rica': '哥斯达黎加', 'Cuba': '古巴', 'Dominican Republic': '多米尼加',
    'Puerto Rico': '波多黎各', 'Jamaica': '牙买加', 'Bolivia': '玻利维亚',
    'Paraguay': '巴拉圭', 'Uruguay': '乌拉圭', 'Cyprus': '塞浦路斯', 'Malta': '马耳他',
    'Moldova': '摩尔多瓦', 'Georgia': '格鲁吉亚', 'Armenia': '亚美尼亚',
    'Azerbaijan': '阿塞拜疆', 'Belarus': '白俄罗斯', 'Cambodia': '柬埔寨', 'Laos': '老挝',
    'Myanmar': '缅甸', 'Afghanistan': '阿富汗', 'Uzbekistan': '乌兹别克斯坦',
    'Tajikistan': '塔吉克斯坦', 'Turkmenistan': '土库曼斯坦', 'Kyrgyzstan': '吉尔吉斯斯坦',
    'Albania': '阿尔巴尼亚', 'Bosnia and Herzegovina': '波黑', 'North Macedonia': '北马其顿',
    'Montenegro': '黑山', 'Mauritius': '毛里求斯', 'Seychelles': '塞舌尔',
}


def _country_zh(en):
    if not en:
        return en
    if en in COUNTRY_ZH:
        return COUNTRY_ZH[en]
    stripped = en[4:] if en.startswith('The ') else en  # ip-api 对荷兰等返回 "The Netherlands"
    return COUNTRY_ZH.get(stripped, en)


def test_proxy(kind=None):
    settings = load_settings()
    kind = kind or available_listener(settings)
    if not kind:
        return {'alive': False, 'ip': '', 'country': ''}
    try:
        r = subprocess.run(
            ['curl', '-x', listener_proxy_url(kind, settings), '-sS', '--connect-timeout', '5', '--max-time', '8',
             'http://ip-api.com/json'],
            capture_output=True, text=True, timeout=12)
        data = json.loads(r.stdout)
        if data.get('status') == 'success':
            return {'alive': True, 'ip': data.get('query', '?'), 'country': _country_zh(data.get('country', ''))}
    except Exception:
        pass
    return {'alive': False, 'ip': '', 'country': ''}


def _speed_download(max_time=12):
    """经当前入口代理实测测速下载（与 /action 测速同一目标同一判定）。
    返回 (结果dict 或 None, 消息)。"""
    try:
        r = subprocess.run(
            ['curl', '-x', listener_proxy_url(available_listener() or 'http'), '-sS', '-o', '/dev/null',
             '-w', '%{http_code} %{size_download} %{speed_download} %{time_total}', '--max-time', str(max_time),
             'https://speed.cloudflare.com/__down?bytes=1048576'],
            capture_output=True, text=True, timeout=max_time + 5)
        parts = r.stdout.strip().split()
        if r.returncode == 0 and len(parts) >= 4 and parts[0] == '200' and float(parts[1]) > 0 and float(parts[2]) > 0:
            speed_bps = float(parts[2])
            latency = float(parts[3]) * 1000
            speed_mbps = (speed_bps * 8) / 1_000_000
            bar = min(100, max(1, int(speed_mbps / 50 * 100)))
            return {'speed': round(speed_mbps, 2), 'latency': round(latency, 1), 'time': '刚刚', 'bar_width': bar}, \
                f'测速完成：{round(speed_mbps, 2)} Mbps'
        if parts and parts[0] == '200':
            return None, '测速失败：测速服务器无数据返回（节点到测速服务器链路异常）'
        return None, f'测速失败（HTTP {parts[0] if parts else "无响应"}）'
    except Exception as ex:
        return None, f'测速失败：{ex}'


def _proxy_usable():
    """入口代理实测：ip-api 探活 + speed.cloudflare 测速下载均通过才算可用。
    与测速使用同一实测目标，避免'验证通过但测速失败'。"""
    if not test_proxy().get('alive'):
        return False
    return _speed_download(8)[0] is not None


def get_mode():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
            if cfg and 'rules' in cfg:
                rule = str(cfg['rules'][0])
                if 'DIRECT' in rule: return '直连'
                if 'REJECT' in rule: return '无可用节点'
                if 'rotate' in rule: return '轮换'
                return '单代理'
    except Exception:
        pass
    return '?'


# ==================== 场景配置生成（非粘性，原有逻辑） ====================
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


def _proxies_from_lines(proxies_lines, proxy_type, username, password):
    proxies = []
    for i, line in enumerate(proxies_lines, 1):
        line = (line or '').strip()
        if not line:
            continue
        parts = line.split(':')
        server = parts[0]
        try:
            port = int(parts[1])
        except (IndexError, ValueError):
            continue
        u = username or (parts[2] if len(parts) > 2 else None)
        p = password or (parts[3] if len(parts) > 3 else None)
        node = {'name': f'p{i}', 'type': proxy_type, 'server': server, 'port': port}
        if u:
            node['username'] = u
        if p:
            node['password'] = p
        proxies.append(node)
    return proxies


def gen_proxy_config(proxy_type, proxies_lines, username, password, rotate):
    proxies = _proxies_from_lines(proxies_lines, proxy_type, username, password)
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
    if 'num=' in api_url:
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


# ==================== mihomo 部署（热重载，不重建容器） ====================
def deploy_mihomo(config_str, scenario_e=False, settings=None):
    settings = settings or load_settings()
    config_str = attach_listeners(config_str, settings)
    return _apply_config(config_str, settings)


def _entry_listeners_ok(settings=None):
    """校验所有启用的对外入口端口是否已监听（mihomo 宿主机 host 网络）。
    SIGHUP 热重载时同端口切换 listener 类型可能触发 mihomo 竞态
    （bind: address already in use），需要回退重启兜底。"""
    settings = settings or load_settings()
    host = MIHOMO_HOST  # 容器内经 host.docker.internal 访问宿主机
    for kind in ('socks', 'http'):
        item = settings[kind]
        if not item.get('enabled'):
            continue
        try:
            s = socket.socket()
            try:
                s.settimeout(2)
                if s.connect_ex((host, int(item['port']))) != 0:
                    return False
            finally:
                s.close()
        except Exception:
            return False
    return True


def _apply_config(config_str, settings=None):
    """写配置 + SIGHUP 热重载；重载后校验入口监听，失败自动回退重启容器"""
    with RELOAD_LOCK:
        settings = settings or load_settings()
        with open(CONFIG_PATH, 'w') as f:
            f.write(config_str)
        try:
            client = get_docker_client()
            c = client.containers.get('mihomo')
            if c.status != 'running':
                logger.warning(f'mihomo 容器未运行（status={c.status}），先启动再重载')
                c.start()
                time.sleep(1)
            c.kill(signal='SIGHUP')
            time.sleep(1.2)
            if not _entry_listeners_ok(settings):
                logger.warning('SIGHUP 热重载后入口未监听（同端口换类型竞态），回退重启 mihomo')
                c.restart()
                time.sleep(2.5)
                if not _entry_listeners_ok(settings):
                    return False, '入口监听校验失败（重启后仍不通）'
            return True, None
        except docker.errors.NotFound:
            pass  # 容器不存在 → 走创建分支
        except Exception as e:
            logger.warning(f'SIGHUP 热重载失败: {type(e).__name__}: {e}')
            return False, f'SIGHUP 失败: {e}'
        try:
            volumes = {CONFIG_PATH: {'bind': '/config.yaml', 'mode': 'ro'},
                       PROVIDERS_PATH: {'bind': '/root/.config/mihomo/providers.yaml', 'mode': 'ro'}}
            get_docker_client().containers.run(
                'metacubex/mihomo:latest', ['-f', '/config.yaml'], name='mihomo', detach=True,
                network_mode='host', restart_policy={'Name': 'always'}, volumes=volumes)
            return True, None
        except Exception as e:
            return False, str(e)


# ==================== 粘性会话核心 ====================
def _sticky_users(settings=None):
    settings = settings or load_settings()
    if settings['socks']['enabled'] and settings['socks']['username']:
        return [{'username': settings['socks']['username'], 'password': settings['socks']['password']}]
    if settings['http']['enabled'] and settings['http']['username']:
        return [{'username': settings['http']['username'], 'password': settings['http']['password']}]
    return []


def _sticky_listeners():
    users = _sticky_users()
    out = []
    for s in STICKY_STATE['sessions'].values():
        listener = {'name': f"sticky-{s['port']}", 'type': 'socks',
                    'listen': '0.0.0.0', 'port': s['port'], 'proxy': s['ref']}
        if users:
            listener['users'] = users
        out.append(listener)
    return out


def _sticky_listen_url(port, host=None):
    settings = load_settings()
    host = host or MIHOMO_HOST
    kind = 'socks' if settings['socks']['enabled'] else 'http'
    item = settings[kind]
    auth = ''
    if item['username']:
        auth = f"{quote(item['username'], safe='')}:{quote(item['password'], safe='')}@"
    scheme = 'socks5h' if kind == 'socks' else 'http'
    return f"{scheme}://{auth}{host}:{port}"


def _alloc_port():
    start = STICKY_STATE['port_start']
    end = STICKY_STATE['port_end']
    settings = load_settings()
    used = set(STICKY_STATE['ports'].keys())
    for item in (settings['socks'], settings['http']):
        if item['enabled']:
            used.add(int(item['port']))
    for p in range(start, end + 1):
        if p not in used:
            return p
    return None


def _test_through_port(port, timeout=8):
    url = STICKY_STATE['test_url']
    try:
        r = subprocess.run(
            ['curl', '-x', _sticky_listen_url(port), '-sS', '--connect-timeout', '5', '--max-time', str(timeout),
             '-o', '/dev/null', '-w', '%{http_code}', url],
            capture_output=True, text=True, timeout=timeout + 5)
        # curl 自身成功（returncode==0）且收到任意 HTTP 状态码，即证明代理链路可达；
        # 404/5xx 等状态码也视为可达（目标服务器响应了），只有连接失败/超时才判定不可用
        return r.returncode == 0 and r.stdout.strip().isdigit()
    except Exception:
        return False


def _rebuild_pool_from_settings(settings=None):
    settings = settings or load_settings()
    p = settings['saved_scenarios'].get('proxy', {})
    ptype = p.get('proxy_type', 'socks5')
    proxies = _proxies_from_lines(p.get('proxies', '').split('\n'), ptype,
                                  p.get('username', ''), p.get('password', ''))
    with STICKY_LOCK:
        STICKY_STATE['pool'] = {}
        for x in proxies:
            key = f"{x['server']}:{x['port']}:{x.get('username', '')}:{x.get('password', '')}"
            STICKY_STATE['pool'][key] = {
                'key': key, 'ref': x['name'], 'proxy': f"{x['server']}:{x['port']}",
                'in_use': 0, 'failures': 0, 'available': True,
            }
        STICKY_STATE['rr'] = 0
    return STICKY_STATE['pool']


def _build_full_config(settings=None):
    """粘性模式完整配置（场景 proxies + 终端/粘性 listeners）"""
    settings = settings or load_settings()
    scenario = settings['scenario']
    base = {'mode': 'rule', 'allow-lan': True, 'bind-address': '*', 'log-level': 'info', 'ipv6': False}
    proxies = []
    groups = []

    if settings.get('exit_mode') == 'direct':
        # 对外出口强制直连：不经过任何上游，直接服务器出口 IP
        base['rules'] = ['MATCH,DIRECT']
    elif scenario == 'A':
        base['rules'] = ['MATCH,DIRECT']
    elif scenario == 'proxy':
        p = settings['saved_scenarios'].get('proxy', {})
        proxies = _proxies_from_lines(p.get('proxies', '').split('\n'), p.get('proxy_type', 'socks5'),
                                      p.get('username', ''), p.get('password', ''))
        if not proxies:
            return None, '代理列表为空'
        if p.get('rotate', 'yes') == 'yes' and len(proxies) > 1:
            groups.append({'name': 'rotate', 'type': 'load-balance', 'strategy': 'round-robin',
                           'proxies': [x['name'] for x in proxies], 'url': 'http://ip-api.com/json', 'interval': 300})
            base['rules'] = ['MATCH,rotate']
        else:
            base['rules'] = ['MATCH,' + proxies[0]['name']]
    elif scenario == 'E':
        for s in list(STICKY_STATE['sessions'].values()):
            if s['scenario'] == 'E' and s.get('proxy_node'):
                proxies.append(s['proxy_node'])
        proxies.extend(list(STICKY_STATE['e_test_nodes']))
        if proxies:
            groups.append({'name': 'rotate', 'type': 'load-balance', 'strategy': 'round-robin',
                           'proxies': [x['name'] for x in proxies], 'url': 'http://ip-api.com/json', 'interval': 60})
            base['rules'] = ['MATCH,rotate']
        else:
            base['rules'] = ['MATCH,REJECT']
    elif scenario == 'F':
        if not STICKY_STATE['f_nodes']:
            _refresh_f_nodes(settings)
        proxies = list(STICKY_STATE['f_nodes'])
        if not proxies:
            return None, '订阅节点为空'
        groups.append({'name': 'rotate', 'type': 'load-balance', 'strategy': 'round-robin',
                       'proxies': [x['name'] for x in proxies], 'url': 'http://ip-api.com/json', 'interval': 60})
        base['rules'] = ['MATCH,rotate']
    else:
        base['rules'] = ['MATCH,DIRECT']

    base['proxies'] = proxies
    if groups:
        base['proxy-groups'] = groups
    base['listeners'] = listener_config(settings) + _sticky_listeners()
    return yaml.safe_dump(base, allow_unicode=True, sort_keys=False), None


def _reload_sticky(settings=None):
    settings = settings or load_settings()
    cfg, err = _build_full_config(settings)
    if err:
        return False, err
    ok, e = _apply_config(cfg, settings)
    time.sleep(0.6)
    return ok, e


def _store_session(session):
    with STICKY_LOCK:
        STICKY_STATE['sessions'][session['task_id']] = session
        STICKY_STATE['ports'][session['port']] = session['task_id']


# ==================== 场景 E：API 提取 ====================
def _parse_proxy_line(line):
    line = line.strip()
    if not line:
        return None
    proxy_type = 'http'
    if '://' in line:
        m = re.match(r'^(socks5h?|http|https)://(.+)$', line)
        if not m:
            return None
        scheme, rest = m.group(1), m.group(2)
        proxy_type = 'socks5' if scheme.startswith('socks5') else 'http'
        if '@' in rest:
            auth, hostport = rest.rsplit('@', 1)
            u, _, pwd = auth.partition(':')
            host, port = hostport.rsplit(':', 1)
            return {'server': host, 'port': int(port), 'type': proxy_type,
                    'username': u, 'password': pwd}
        host, port = rest.rsplit(':', 1)
        return {'server': host, 'port': int(port), 'type': proxy_type}
    parts = line.split(':')
    if len(parts) < 2:
        return None
    node = {'server': parts[0], 'port': int(parts[1]), 'type': proxy_type}
    if len(parts) > 3:
        node['username'] = parts[2]
        node['password'] = parts[3]
    return node


def _extract_from_api(api_url, num=1):
    url = api_url
    if 'num=' in url:
        url = re.sub(r'num=\d+', f'num={num}', url)
    elif '?' in url:
        url = url + f'&num={num}'
    else:
        url = url + f'?num={num}'
    try:
        r = subprocess.run(['curl', '-sS', '--connect-timeout', '10', '--max-time', '15', url],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return []
        return [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    except Exception:
        return []


# ==================== 场景 F：订阅解析 ====================
def _b64decode_str(s):
    s = s.strip().replace('-', '+').replace('_', '/')
    pad = '=' * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad).decode('utf-8', errors='replace')
    except Exception:
        return ''


def _parse_ss_uri(uri):
    rest = uri[len('ss://'):]
    name = ''
    if '#' in rest:
        rest, name = rest.rsplit('#', 1)
        try:
            name = urllib.parse.unquote(name)
        except Exception:
            pass
    if '@' in rest:
        auth, hostport = rest.rsplit('@', 1)
        if ':' in auth and not auth.endswith(':'):
            method, password = auth.split(':', 1)
        else:
            dec = _b64decode_str(auth)
            method, password = (dec.split(':', 1) if ':' in dec else ('aes-128-gcm', dec))
        host, port = hostport.rsplit(':', 1)
        try:
            port = int(port)
        except ValueError:
            return None
        return {'name': name or f'ss-{host}-{port}', 'type': 'ss', 'server': host,
                'port': port, 'cipher': method, 'password': password}
    dec = _b64decode_str(rest)
    if '#' in dec:
        dec, name = dec.rsplit('#', 1)
    if '@' in dec:
        auth, hostport = dec.rsplit('@', 1)
        method, password = (auth.split(':', 1) if ':' in auth else ('aes-128-gcm', auth))
        host, port = hostport.rsplit(':', 1)
        try:
            port = int(port)
        except ValueError:
            return None
        return {'name': name or f'ss-{host}-{port}', 'type': 'ss', 'server': host,
                'port': port, 'cipher': method, 'password': password}
    return None


def _parse_vmess_uri(uri):
    payload = uri[len('vmess://'):]
    dec = _b64decode_str(payload)
    try:
        if dec.startswith('{'):
            j = json.loads(dec)
            host = j.get('add') or j.get('host')
            port = int(j.get('port', 0))
            if not host or not port:
                return None
            node = {'name': j.get('ps') or f"vmess-{host}-{port}", 'type': 'vmess',
                    'server': host, 'port': port, 'uuid': j.get('id'), 'alterId': int(j.get('aid', 0)),
                    'cipher': j.get('scy') or 'auto'}
            net = j.get('net', 'tcp')
            if net == 'ws':
                ws = {'path': j.get('path') or '/'}
                if j.get('host'):
                    ws['headers'] = {'Host': j['host']}
                node['network'] = 'ws'
                node['ws-opts'] = ws
            elif net == 'grpc':
                node['network'] = 'grpc'
                node['grpc-opts'] = {'grpc-service-name': j.get('path') or ''}
            elif net not in ('tcp', ''):
                node['network'] = net
            if j.get('tls') == 'tls':
                node['tls'] = True
                node['servername'] = j.get('sni') or j.get('host') or host
                node['client-fingerprint'] = j.get('fp') or 'chrome'
            return node
        rest = dec
        if '#' in rest:
            rest, name = rest.rsplit('#', 1)
        else:
            name = ''
        host, port = rest.rsplit(':', 1)
        return {'name': name or f'vmess-{host}-{port}', 'type': 'vmess',
                'server': host, 'port': int(port), 'uuid': '', 'alterId': 0, 'cipher': 'auto'}
    except Exception:
        return None


def _parse_vless_trojan_uri(uri, proto):
    rest = uri[len(proto + '://'):]
    name = ''
    if '#' in rest:
        rest, name = rest.rsplit('#', 1)
        try:
            name = urllib.parse.unquote(name)
        except Exception:
            pass
    params = {}
    if '?' in rest:
        rest, qs = rest.split('?', 1)
        for kv in qs.split('&'):
            if '=' in kv:
                k, v = kv.split('=', 1)
                params[k] = urllib.parse.unquote(v)
    if '@' in rest:
        password, hostport = rest.rsplit('@', 1)
        host, port = hostport.rsplit(':', 1)
    else:
        return None
    try:
        port = int(port)
    except ValueError:
        return None
    node = {'name': name or f'{proto}-{host}-{port}', 'type': proto, 'server': host, 'port': port}
    if proto == 'vless':
        node['uuid'] = password
    else:
        node['password'] = password
    if params.get('security') and params['security'] != 'none':
        node['tls'] = True
        node['servername'] = params.get('sni') or params.get('host') or host
        node['client-fingerprint'] = 'chrome'
        if params.get('alpn'):
            node['alpn'] = [params['alpn']]
    if params.get('type') in ('ws', 'grpc', 'h2', 'http'):
        node['network'] = params['type']
    if params.get('type') == 'ws':
        ws = {'path': params.get('path') or '/'}
        if params.get('host'):
            ws['headers'] = {'Host': params['host']}
        node['ws-opts'] = ws
    elif params.get('type') == 'grpc':
        node['grpc-opts'] = {'grpc-service-name': params.get('serviceName') or params.get('path') or ''}
    return node


def _parse_hysteria2_uri(uri):
    rest = uri[len('hysteria2://'):]
    name = ''
    if '#' in rest:
        rest, name = rest.rsplit('#', 1)
        try:
            name = urllib.parse.unquote(name)
        except Exception:
            pass
    params = {}
    if '?' in rest:
        rest, qs = rest.split('?', 1)
        for kv in qs.split('&'):
            if '=' in kv:
                k, v = kv.split('=', 1)
                params[k] = urllib.parse.unquote(v)
    if '@' in rest:
        password, hostport = rest.rsplit('@', 1)
        host, port = hostport.rsplit(':', 1)
    else:
        return None
    try:
        port = int(port)
    except ValueError:
        return None
    node = {'name': name or f'hysteria2-{host}-{port}', 'type': 'hysteria2',
            'server': host, 'port': port, 'password': password}
    if params.get('sni'):
        node['sni'] = params['sni']
    if params.get('insecure') == '1':
        node['skip-cert-verify'] = True
    return node


def _parse_subscription(content):
    nodes = []
    stripped = content.strip()
    if not stripped:
        return []
    if 'proxies:' in stripped:
        try:
            cfg = yaml.safe_load(content)
            proxies = cfg.get('proxies', [])
            for i, p in enumerate(proxies):
                if not isinstance(p, dict) or 'server' not in p or 'port' not in p:
                    continue
                node = dict(p)
                node.setdefault('name', f"n{i+1}")
                nodes.append(node)
            if nodes:
                return nodes
        except Exception:
            pass
    dec = _b64decode_str(stripped)
    lines = dec.splitlines() if dec else stripped.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('//'):
            continue
        try:
            if line.startswith('vmess://'):
                n = _parse_vmess_uri(line)
            elif line.startswith('vless://'):
                n = _parse_vless_trojan_uri(line, 'vless')
            elif line.startswith('trojan://'):
                n = _parse_vless_trojan_uri(line, 'trojan')
            elif line.startswith('ss://'):
                n = _parse_ss_uri(line)
            elif line.startswith('hysteria2://') or line.startswith('hy2://'):
                n = _parse_hysteria2_uri(line)
            elif line.startswith('socks5://') or line.startswith('http://'):
                p = _parse_proxy_line(line)
                n = None
                if p:
                    n = {'name': f"n{len(nodes)+1}", 'type': p['type'], 'server': p['server'], 'port': p['port']}
                    if p.get('username'):
                        n['username'] = p['username']
                    if p.get('password'):
                        n['password'] = p['password']
            else:
                n = None
            if n:
                nodes.append(n)
        except Exception:
            continue
    seen = set()
    unique = []
    for n in nodes:
        key = f"{n.get('server')}:{n.get('port')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(n)
    return unique


def _fetch_subscription(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': 'clash-verge/v1.0.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def _refresh_f_nodes(settings=None):
    settings = settings or load_settings()
    f = settings['saved_scenarios'].get('F', {})
    url = f.get('clash_url', '')
    if not url:
        return
    try:
        content = _fetch_subscription(url)
        nodes = _parse_subscription(content)
        if nodes:
            with STICKY_LOCK:
                STICKY_STATE['f_nodes'] = nodes
                STICKY_STATE['f_updated'] = time.time()
                STICKY_STATE['f_fail'] = {}
                STICKY_STATE['rr'] = 0
            logger.info(f'订阅刷新成功，节点数: {len(nodes)}')
    except Exception as e:
        logger.warning(f'订阅刷新失败: {e}')


# ==================== 会话管理 ====================
def acquire_session(task_id):
    task_id = (task_id or '').strip()
    if not task_id:
        return None, 'task_id 不能为空'
    if not STICKY_STATE['enabled']:
        return None, '粘性会话模式未开启'
    settings = load_settings()
    with STICKY_LOCK:
        if task_id in STICKY_STATE['sessions']:
            return STICKY_STATE['sessions'][task_id], None
    scenario = settings['scenario']
    logger.info(f'acquire 请求 task_id={task_id} scenario={scenario} 池大小={len(STICKY_STATE["pool"])}')
    if scenario == 'A':
        return None, '场景A为直连模式，无需粘性会话，请直接使用 7890/7891 端口'
    if scenario == 'proxy':
        return _acquire_bcd(task_id)
    if scenario == 'E':
        return _acquire_e(task_id)
    if scenario == 'F':
        return _acquire_f(task_id)
    return None, f'场景 {scenario} 暂不支持粘性会话'


def _reset_pool_availability():
    """把池内所有代理重置为可用（failures=0），使故障恢复后能重新被分配。
    保留 in_use 计数；坏代理会在下次 acquire 实测时再次被排除。"""
    with STICKY_LOCK:
        for v in STICKY_STATE['pool'].values():
            v['available'] = True
            v['failures'] = 0


def _acquire_bcd(task_id):
    """场景 B/C/D：轮询分配（不限制并发），绑定后故障自动切换"""
    settings = load_settings()
    if not STICKY_STATE['pool']:
        _rebuild_pool_from_settings(settings)
    start = time.time()
    last_reset = 0
    attempts = 0
    while True:
        with STICKY_LOCK:
            avail = [k for k, v in STICKY_STATE['pool'].items() if v['available']]
        if not avail:
            if time.time() - start >= STICKY_STATE['queue_timeout']:
                return None, '排队超时，无可用代理'
            # 代理故障恢复后自动重新探测：排队期间周期重置可用性，
            # 否则不可用代理被排除后永远无法恢复
            if time.time() - last_reset >= 15:
                _reset_pool_availability()
                last_reset = time.time()
            time.sleep(1)
            continue
        idx = STICKY_STATE['rr'] % len(avail)
        with STICKY_LOCK:
            STICKY_STATE['rr'] += 1
        key = avail[idx]
        with STICKY_LOCK:
            info = STICKY_STATE['pool'][key]
            info['in_use'] += 1
            ref, proxy = info['ref'], info['proxy']
        port = _alloc_port()
        if port is None:
            with STICKY_LOCK:
                STICKY_STATE['pool'][key]['in_use'] -= 1
            return None, '端口已用尽'
        session = {'task_id': task_id, 'proxy': proxy, 'proxy_key': key, 'ref': ref,
                   'port': port, 'scenario': settings['scenario'], 'created': time.time(),
                   'expires': None, 'failures': 0, 'status': 'active', 'proxy_node': None}
        _store_session(session)
        attempts += 1
        ok, err = _reload_sticky(settings)
        if not ok:
            release_session(task_id)
            return None, f'热重载失败：{err}'
        if STICKY_STATE['test_enabled']:
            if _test_through_port(port):
                logger.info(f'分配成功 task_id={task_id} proxy={proxy} port={port}')
                return session, None
            with STICKY_LOCK:
                STICKY_STATE['pool'][key]['in_use'] -= 1
                STICKY_STATE['pool'][key]['failures'] += 1
                if STICKY_STATE['pool'][key]['failures'] >= 3:
                    STICKY_STATE['pool'][key]['available'] = False
            release_session(task_id)
            # 不设 attempts 上限：失败计数会逐步排除所有不可用节点，
            # 全部排除后 avail 为空自然进入排队等待（queue_timeout 超时返回）
            continue
        logger.info(f'分配成功 task_id={task_id} proxy={proxy} port={port}')
        return session, None


def _acquire_e(task_id):
    """场景 E：懒加载提取，1任务1IP，10分钟过期，失败不切换"""
    settings = load_settings()
    f = settings['saved_scenarios'].get('E', {})
    api_url = f.get('api_url', '').strip()
    if not api_url:
        return None, '请先配置 API URL'
    port = _alloc_port()
    if port is None:
        return None, '端口已用尽'
    last_err = '提取失败'
    for attempt in range(3):
        lines = _extract_from_api(api_url, 1)
        if not lines:
            last_err = f'代理提取失败，已重试{attempt+1}次'
            time.sleep(1)
            continue
        node = None
        for line in lines:
            n = _parse_proxy_line(line)
            if n:
                node = n
                break
        if not node:
            last_err = 'API 返回格式无法解析'
            continue
        node['name'] = f"e_{port}"
        session = {'task_id': task_id, 'proxy': f"{node['server']}:{node['port']}",
                   'proxy_key': None, 'ref': node['name'], 'port': port,
                   'scenario': 'E', 'created': time.time(),
                   'expires': time.time() + STICKY_STATE['timeout'],
                   'failures': 0, 'status': 'active', 'proxy_node': node}
        _store_session(session)
        ok, err = _reload_sticky(settings)
        if not ok:
            release_session(task_id)
            return None, f'热重载失败：{err}'
        if STICKY_STATE['test_enabled']:
            if _test_through_port(port):
                logger.info(f'E 提取成功 task_id={task_id} proxy={session["proxy"]} port={port}')
                return session, None
            last_err = f'代理验证失败，已重试{attempt+1}次'
            logger.warning(f'E 提取后验证失败 task_id={task_id} 第{attempt+1}次')
            release_session(task_id)
            time.sleep(1)
            continue
        logger.info(f'E 提取成功（未验证）task_id={task_id} proxy={session["proxy"]} port={port}')
        return session, None
    _free_port(port)
    return None, last_err


def _ensure_e_proxy(settings=None, need_speed=False):
    """E 场景 + 粘性开启：确保有可用测试节点，使 7890/7891 测速/测试不显示空。
    need_speed=True（测速）：提取后经入口实测（ip-api 探活 + speed.cloudflare 下载，
    与测速同一目标），不合格自动换节点重试（最多3次）；
    need_speed=False（测试代理/切换）：仅 ip-api 探活，快速返回。
    提取 3 次全部失败后 60 秒冷却，避免反复点击长时间等待。"""
    settings = settings or load_settings()
    if settings.get('scenario') != 'E' or not STICKY_STATE['enabled']:
        return False, '非 E 场景或粘性未开启'
    with STICKY_LOCK:
        if STICKY_STATE['e_extract_cd'] > time.time():
            return False, STICKY_STATE['e_extract_cd_err'] or '测试节点提取冷却中'
    if STICKY_STATE['e_test_nodes']:
        usable = _proxy_usable() if need_speed else test_proxy().get('alive')
        if usable:
            return True, None
        with STICKY_LOCK:
            STICKY_STATE['e_test_nodes'] = []
        logger.warning('E 已有测试节点不可用，重新提取')
    with STICKY_LOCK:
        if any(s.get('scenario') == 'E' and s.get('proxy_node')
               for s in STICKY_STATE['sessions'].values()):
            return True, None
    f = settings.get('saved_scenarios', {}).get('E', {})
    api_url = f.get('api_url', '').strip()
    if not api_url:
        return False, 'E 场景未配置 API URL'
    # 测试代理/切换只提取 1 次快速返回；测速才做 3 次换节点尝试
    attempts = 3 if need_speed else 1
    for attempt in range(attempts):
        lines = _extract_from_api(api_url, 1)
        node = None
        for line in lines:
            n = _parse_proxy_line(line)
            if n:
                node = n
                break
        if not node:
            logger.warning(f'E 测试节点第{attempt+1}次提取为空')
            time.sleep(1)
            continue
        node['name'] = 'e_test'
        with STICKY_LOCK:
            STICKY_STATE['e_test_nodes'] = [node]
        ok, err = _reload_sticky(settings)
        if not ok:
            with STICKY_LOCK:
                STICKY_STATE['e_test_nodes'] = []
            return False, f'热重载失败：{err}'
        usable = _proxy_usable() if need_speed else test_proxy().get('alive')
        if usable:
            logger.info(f'E 自动提取测试节点 proxy={node["server"]}:{node["port"]}')
            return True, None
        with STICKY_LOCK:
            STICKY_STATE['e_test_nodes'] = []
        logger.warning(f'E 测试节点第{attempt+1}次验证失败，重新提取')
        time.sleep(1)
    # 3 次全部失败：设置冷却，避免反复点击长时间等待
    with STICKY_LOCK:
        STICKY_STATE['e_extract_cd'] = time.time() + 60
        STICKY_STATE['e_extract_cd_err'] = '提取的测试节点验证均失败（经实测均不可用）'
    return False, '提取的测试节点验证均失败（经实测均不可用）'


def _free_port(port):
    with STICKY_LOCK:
        STICKY_STATE['ports'].pop(port, None)


def _f_available_nodes():
    with STICKY_LOCK:
        nodes = list(STICKY_STATE['f_nodes'])
        f_fail = dict(STICKY_STATE['f_fail'])
    return [n for n in nodes if f_fail.get(n['name'], 0) < 3]


def _acquire_f(task_id):
    """场景 F：直连（绑定第一个可用，不过期，故障切换）/ 轮询（10分钟过期）"""
    settings = load_settings()
    if not STICKY_STATE['f_nodes']:
        _refresh_f_nodes(settings)
    if not STICKY_STATE['f_nodes']:
        return None, '订阅节点为空'
    mode = STICKY_STATE['f_mode']
    attempts = 0
    while True:
        nodes = _f_available_nodes()
        if not nodes:
            if mode == 'poll' and STICKY_STATE['f_nodes']:
                nodes = [STICKY_STATE['f_nodes'][0]]
            if not nodes:
                return None, '所有节点均不可用'
        idx = STICKY_STATE['rr'] % len(nodes)
        with STICKY_LOCK:
            STICKY_STATE['rr'] += 1
        node = nodes[idx]
        ref = node['name']
        port = _alloc_port()
        if port is None:
            return None, '端口已用尽'
        session = {'task_id': task_id, 'proxy': f"{node.get('server')}:{node.get('port')}",
                   'proxy_key': None, 'ref': ref, 'port': port, 'scenario': 'F',
                   'created': time.time(),
                   'expires': time.time() + STICKY_STATE['timeout'] if mode == 'poll' else None,
                   'failures': 0, 'status': 'active', 'proxy_node': None}
        _store_session(session)
        attempts += 1
        ok, err = _reload_sticky(settings)
        if not ok:
            release_session(task_id)
            return None, f'热重载失败：{err}'
        if STICKY_STATE['test_enabled']:
            if _test_through_port(port):
                logger.info(f'F 分配成功 task_id={task_id} node={ref} port={port} mode={mode}')
                return session, None
            if mode == 'direct':
                with STICKY_LOCK:
                    STICKY_STATE['f_fail'][ref] = STICKY_STATE['f_fail'].get(ref, 0) + 1
                logger.warning(f'F 直连节点 {ref} 不可用，失败计数+1 task_id={task_id}')
                release_session(task_id)
                if attempts >= len(STICKY_STATE['f_nodes']):
                    return None, '遍历所有节点均不可用'
                continue
            # 轮询模式：测试失败则计数+1并跳过，试下一个；全部失败则复用
            with STICKY_LOCK:
                STICKY_STATE['f_fail'][ref] = STICKY_STATE['f_fail'].get(ref, 0) + 1
            release_session(task_id)
            if attempts >= len(STICKY_STATE['f_nodes']):
                return _acquire_f_reuse(task_id)
            continue
        logger.info(f'F 分配成功（未验证）task_id={task_id} node={ref} port={port}')
        return session, None


def _acquire_f_reuse(task_id):
    """F 轮询：节点不够时复用已分配节点"""
    with STICKY_LOCK:
        if not STICKY_STATE['f_nodes']:
            return None, '订阅节点为空'
        node = STICKY_STATE['f_nodes'][0]
    port = _alloc_port()
    if port is None:
        return None, '端口已用尽'
    session = {'task_id': task_id, 'proxy': f"{node.get('server')}:{node.get('port')}",
               'proxy_key': None, 'ref': node['name'], 'port': port, 'scenario': 'F',
               'created': time.time(), 'expires': time.time() + STICKY_STATE['timeout'],
               'failures': 0, 'status': 'active', 'proxy_node': None}
    _store_session(session)
    ok, err = _reload_sticky(load_settings())
    if not ok:
        release_session(task_id)
        return None, f'热重载失败：{err}'
    logger.info(f'F 轮询复用节点 task_id={task_id} node={node["name"]} port={port}')
    return session, None


def release_session(task_id):
    with STICKY_LOCK:
        s = STICKY_STATE['sessions'].pop(task_id, None)
        if not s:
            return None, '会话不存在'
        STICKY_STATE['ports'].pop(s['port'], None)
        info = STICKY_STATE['pool'].get(s.get('proxy_key'))
        if info:
            info['in_use'] = max(0, info['in_use'] - 1)
    settings = load_settings()
    ok, err = _reload_sticky(settings)
    logger.info(f'release task_id={task_id} port={s["port"]} proxy={s.get("proxy")} ok={ok}')
    if not ok:
        return None, f'热重载失败：{err}'
    return s, None


def rotate_session(task_id):
    """手动切换会话绑定到下一个可用代理/节点（端口不变）"""
    with STICKY_LOCK:
        s = STICKY_STATE['sessions'].get(task_id)
        if not s:
            return None, '会话不存在'
        scenario = s['scenario']
        old_ref = s['ref']
    if scenario in ('B', 'C', 'D', 'proxy'):
        with STICKY_LOCK:
            pool = STICKY_STATE['pool']
            keys = [k for k, v in pool.items() if v['available'] and v['ref'] != old_ref]
        if not keys:
            return None, '没有其他可用代理'
        key = keys[0]
        with STICKY_LOCK:
            info = pool[key]
            info['in_use'] += 1
            for k, v in pool.items():
                if v['ref'] == old_ref:
                    v['in_use'] = max(0, v['in_use'] - 1)
                    break
            s['ref'] = info['ref']
            s['proxy'] = info['proxy']
            s['proxy_key'] = key
        ok, err = _reload_sticky()
        if not ok:
            return None, f'热重载失败：{err}'
        if STICKY_STATE['test_enabled'] and not _test_through_port(s['port']):
            return s, '已切换但新代理测试失败'
        return s, None
    if scenario == 'F' and STICKY_STATE['f_mode'] == 'direct':
        nodes = _f_available_nodes()
        candidates = [n for n in nodes if n['name'] != old_ref]
        if not candidates:
            return None, '没有其他可用节点'
        for node in candidates:
            with STICKY_LOCK:
                s['ref'] = node['name']
                s['proxy'] = f"{node.get('server')}:{node.get('port')}"
            ok, err = _reload_sticky()
            if not ok:
                return None, f'热重载失败：{err}'
            if STICKY_STATE['test_enabled']:
                if _test_through_port(s['port']):
                    return s, None
                with STICKY_LOCK:
                    STICKY_STATE['f_fail'][node['name']] = STICKY_STATE['f_fail'].get(node['name'], 0) + 1
                continue
            return s, None
        return None, '所有候选节点均不可用'
    return None, '该场景会话不支持手动切换'


def _clear_sessions(reason='场景切换'):
    with STICKY_LOCK:
        tasks = list(STICKY_STATE['sessions'].keys())
    for t in tasks:
        release_session(t)
    logger.info(f'清空会话：{reason}')


# ==================== 后台维护线程 ====================
def _sticky_cleanup():
    if not STICKY_STATE['enabled']:
        return
    now = time.time()
    with STICKY_LOCK:
        expired = [t for t, s in STICKY_STATE['sessions'].items()
                   if s.get('expires') and now >= s['expires']]
    for t in expired:
        s, _ = release_session(t)
        logger.info(f'过期清理 task_id={t}')


def _sticky_health_check():
    if not STICKY_STATE['enabled']:
        return
    with STICKY_LOCK:
        snap = [(t, s['port'], s['scenario'], s['ref']) for t, s in STICKY_STATE['sessions'].items()]
    for t, port, scenario, ref in snap:
        try:
            if scenario in ('B', 'C', 'D', 'proxy', 'F') and not _test_through_port(port):
                if scenario == 'F' and STICKY_STATE['f_mode'] != 'direct':
                    continue
                logger.warning(f'健康检查失败 session={t} proxy={ref}，尝试切换')
                rotate_session(t)
        except Exception as e:
            logger.warning(f'健康检查异常 session={t}: {e}')


def _sticky_maintenance_loop():
    last_health = 0
    last_fref = 0
    while True:
        try:
            _sync_sticky_from_settings()
            _sticky_cleanup()
            now = time.time()
            if STICKY_STATE['enabled']:
                if now - last_health >= 60:
                    _sticky_health_check()
                    last_health = now
                if now - last_fref >= 600:
                    _refresh_f_nodes(load_settings())
                    last_fref = now
        except Exception as e:
            logger.warning(f'维护线程异常: {e}')
        time.sleep(30)


threading.Thread(target=_sticky_maintenance_loop, daemon=True).start()


# ==================== WebUI 渲染 ====================
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
    sticky = {
        'enabled': STICKY_STATE['enabled'],
        'test_url': STICKY_STATE['test_url'],
        'test_enabled': STICKY_STATE['test_enabled'],
        'timeout': STICKY_STATE['timeout'],
    }
    pool = STICKY_STATE['pool']
    pool_total = len(pool)
    pool_available = sum(1 for v in pool.values() if v['available'])
    pool_in_use = sum(v['in_use'] for v in pool.values())
    sessions = [_session_public(s) for s in STICKY_STATE['sessions'].values()]
    sessions.sort(key=lambda x: x['acquired_at'])
    # 概览页快速开始数据
    mode_labels = {'mixed': 'SOCKS5/HTTP 混合', 'socks': 'SOCKS5', 'http': 'HTTP', 'dual': 'SOCKS5 + HTTP'}
    settings.setdefault('entry_mode_label', mode_labels.get(settings.get('entry_mode'), settings.get('entry_mode', '')))
    conn_urls = {}
    for kind in ('socks', 'http'):
        item = settings.get(kind, {})
        scheme = 'socks5' if kind == 'socks' else 'http'
        url = f"{scheme}://{public_host}:{item.get('port', 7890)}"
        masked = url
        if item.get('username'):
            auth = f"{item['username']}:********@"
            masked = f"{scheme}://{auth}{public_host}:{item.get('port', 7890)}"
        conn_urls[f'{kind}_masked'] = masked
    conn_ready = bool(settings.get('socks', {}).get('username') or settings.get('http', {}).get('username'))
    context.update(settings=settings, settings_public=settings_public, public_host=public_host,
                   sticky=sticky, pool={'total': pool_total, 'available': pool_available,
                                        'in_use': pool_in_use}, sessions=sessions,
                   api_key=settings.get('api_key', ''), conn_urls=conn_urls, conn_ready=conn_ready)
    return render_template_string(HTML, **context)


def _session_public(s):
    host = request.host.split(':', 1)[0] if request else ''
    return {
        'task_id': s['task_id'], 'proxy': s.get('proxy', ''),
        'listener': f"{host}:{s['port']}",
        'listener_port': s['port'],
        'acquired_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(s['created'])),
        'expires_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(s['expires'])) if s.get('expires') else None,
        'status': s.get('status', 'active'),
        'scenario': s['scenario'],
    }


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('authed'):
            return redirect(url_for('index'))
        return fn(*args, **kwargs)
    return wrapped


@app.route('/assets/<path:filename>')
def react_assets(filename):
    """React 构建产物请求 /assets/...，映射到 static/assets 目录。"""
    return send_from_directory(os.path.join(app.static_folder or 'static', 'assets'), filename)


@app.route('/favicon.svg')
def react_favicon():
    return send_from_directory(app.static_folder or 'static', 'favicon.svg')


@app.route('/')
def index():
    """优先托管 React 前端（前端自行处理登录态）；static/index.html 不存在时回退旧模板。"""
    idx = os.path.join(app.static_folder or 'static', 'index.html')
    if os.path.exists(idx):
        return send_from_directory(app.static_folder or 'static', 'index.html')
    authed = bool(session.get('authed'))
    status = test_proxy() if authed else {'alive': False}
    if authed:
        status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg = f.read()
    except Exception:
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


@app.route('/apply', methods=['POST'])
@login_required
def apply():
    scenario = request.form.get('scenario')
    is_switch = request.form.get('switch') == '1'
    settings = load_settings()
    saved = settings.get('saved_scenarios', {})
    msg, ok = '', False

    if is_switch:
        params = saved.get(scenario, {})
        if not params and scenario != 'A':
            msg = '没有已保存的配置，请先保存应用'
        elif scenario == 'A':
            params = {'configured': True}
    else:
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
                'mode': request.form.get('f_mode', 'direct'),
            }
        elif scenario == 'A':
            params = {'configured': True}
        saved[scenario] = params
        settings['saved_scenarios'] = saved

    if not msg:
        if STICKY_STATE['enabled']:
            ok = True
            msg = f'场景参数已保存（粘性模式已开启）'
        elif scenario == 'A':
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

    if ok:
        settings['scenario'] = scenario
        save_settings(settings)
        if STICKY_STATE['enabled']:
            _clear_sessions()
            _sync_sticky_from_settings(settings)
            if scenario == 'proxy':
                _rebuild_pool_from_settings(settings)
            elif scenario == 'F':
                _refresh_f_nodes(settings)
            if scenario != 'E':
                with STICKY_LOCK:
                    STICKY_STATE['e_test_nodes'] = []
            ok2, err2 = _reload_sticky(settings)
            if not ok2:
                msg = f'粘性配置应用失败：{err2}'
                ok = False
            else:
                msg = '场景已切换（粘性模式），已重建配置'
                if scenario == 'E':
                    _, e_err = _ensure_e_proxy(settings)
                    msg = '场景已切换（粘性模式），已自动提取测试节点' if not e_err else f'场景已切换，但测试节点提取失败：{e_err}'

    time.sleep(1)
    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except Exception:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api=params.get('api_url', ''), speed_result=None)


@app.route('/action', methods=['POST'])
@login_required
def action():
    act = request.form.get('act')
    msg, ok = '', True
    speed_result = None

    if act == 'test':
        e_err = None
        if STICKY_STATE['enabled'] and load_settings().get('scenario') == 'E':
            _, e_err = _ensure_e_proxy()
        status = test_proxy()
        if not status['alive']:
            time.sleep(1)
            status = test_proxy()  # 热重载后首次探测可能瞬时失败，重试一次
        status['mode'] = get_mode()
        if status['alive']:
            msg = f'代理正常，出口 IP: {status["ip"]} ({status["country"]})'
        else:
            msg = f'代理不可用{e_err or ""}'
            ok = False
        try:
            with open(CONFIG_PATH) as f:
                cfg_text = f.read()
        except Exception:
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
        e_err = None
        if STICKY_STATE['enabled'] and load_settings().get('scenario') == 'E':
            with STICKY_LOCK:
                has_node = bool(STICKY_STATE['e_test_nodes']) or any(
                    s.get('scenario') == 'E' and s.get('proxy_node')
                    for s in STICKY_STATE['sessions'].values())
            if not has_node:
                _, e_err = _ensure_e_proxy(need_speed=True)

        speed_result, msg = None, ''
        if e_err:
            # 自动提取测试节点失败：直接给出原因，不再做无意义测速
            msg = f'测速失败：{e_err}'
        else:
            speed_result, msg = _speed_download(12)
        ok = speed_result is not None
        # 失败且 E 场景有测试节点时：强制换节点重试一次
        if not ok and not e_err and STICKY_STATE['enabled'] and load_settings().get('scenario') == 'E':
            with STICKY_LOCK:
                STICKY_STATE['e_test_nodes'] = []
            _, e_err = _ensure_e_proxy(need_speed=True)
            if e_err:
                msg = f'测速失败：{e_err}'
            else:
                speed_result, msg = _speed_download(12)
                ok = speed_result is not None

    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except Exception:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api='https://api.cliproxy.io/white/api?region=Rand&num=10&time=10&format=n&type=txt',
                       speed_result=speed_result)


@app.route('/terminal-settings', methods=['POST'])
@login_required
def terminal_settings():
    old = load_settings()
    terminal_action = request.form.get('terminal_action')
    entry_mode = request.form.get('entry_mode') or old.get('entry_mode', 'dual')
    exit_mode = request.form.get('exit_mode') or old.get('exit_mode', 'scenario')
    if terminal_action == 'direct':  # 兼容旧按钮
        exit_mode = 'direct'
    settings = {
        'scenario': old['scenario'],
        'saved_scenarios': old.get('saved_scenarios', {}),
        'sticky': old.get('sticky', {}),
        'api_key': request.form.get('api_key', '').strip() or old['api_key'],
        'entry_mode': entry_mode, 'exit_mode': exit_mode,
    }
    socks, http = dict(old['socks']), dict(old['http'])
    if entry_mode in ('mixed', 'socks', 'http'):
        # 统一入口：一套端口 + 一套账号密码
        port = request.form.get('entry_port', '').strip()
        username = request.form.get('entry_username', '').strip()
        password = request.form.get('entry_password', '') or old['socks']['password']
        if entry_mode == 'http':
            http.update(enabled=True, port=port, username=username, password=password)
            socks.update(enabled=False)
        else:  # mixed / socks
            socks.update(enabled=True, port=port, username=username, password=password)
            http.update(enabled=False)
    else:  # dual 双入口各自配置
        for kind in ('socks', 'http'):
            entered_password = request.form.get(f'{kind}_password', '')
            item = dict(old[kind])
            item.update(
                enabled=request.form.get(f'{kind}_enabled') == 'on',
                port=request.form.get(f'{kind}_port', ''),
                username=request.form.get(f'{kind}_username', '').strip(),
                password=entered_password if entered_password else item['password'],
            )
            if not item['username']:
                item['password'] = ''
            (socks if kind == 'socks' else http).update(item)
    settings['socks'], settings['http'] = socks, http
    error = validate_terminal_settings(settings)
    ok, msg = False, error or ''
    if not error:
        if STICKY_STATE['enabled']:
            ok, deploy_error = _reload_sticky(settings)
            if ok:
                save_settings(settings)
                msg = '对外连接设置已保存并应用（粘性模式）'
            else:
                msg = f'应用失败：{deploy_error}'
        else:
            if exit_mode == 'direct':
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
                if test_kind == 'main':  # 统一入口模式：映射到实际协议
                    test_kind = 'http' if entry_mode == 'http' else 'socks'
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


# ==================== 粘性模式 WebUI 控制 ====================
@app.route('/sticky-toggle', methods=['POST'])
@login_required
def sticky_toggle():
    settings = load_settings()
    sticky = settings.get('sticky', {})
    sticky['enabled'] = not sticky.get('enabled', False)
    settings['sticky'] = sticky
    save_settings(settings)
    _sync_sticky_from_settings(settings)
    msg, ok = '', True
    if sticky['enabled']:
        # 开启粘性：移除 E 刷新 crontab（避免定时重启容器），重建池/订阅
        subprocess.run('bash -c \'crontab -l 2>/dev/null | grep -v cliproxy_refresh | crontab -\'', shell=True, capture_output=True)
        scenario = settings['scenario']
        if scenario == 'proxy':
            _rebuild_pool_from_settings(settings)
        elif scenario == 'F':
            _refresh_f_nodes(settings)
        ok2, err = _reload_sticky(settings)
        if not ok2:
            msg, ok = f'粘性模式开启失败：{err}', False
        else:
            msg = '粘性会话模式已开启'
    else:
        # 关闭粘性：清空会话，恢复原场景配置
        _clear_sessions('关闭粘性模式')
        if settings['scenario'] == 'E':
            cfg = gen_api_config(settings['saved_scenarios'].get('E', {}).get('api_url', ''), '1')
            ok, err = deploy_mihomo(cfg, scenario_e=True, settings=settings)
        else:
            current = gen_direct_config()
            if settings['scenario'] == 'proxy':
                p = settings['saved_scenarios'].get('proxy', {})
                cfg, err = gen_proxy_config(p.get('proxy_type', 'socks5'), p.get('proxies', '').split('\n'),
                                            p.get('username', ''), p.get('password', ''), p.get('rotate', 'yes'))
                current = cfg
            elif settings['scenario'] == 'F':
                current = gen_subscription_config(settings['saved_scenarios'].get('F', {}).get('clash_url', ''))
            ok, err = deploy_mihomo(current, settings=settings)
        msg = '粘性会话模式已关闭，恢复原场景' if ok else f'关闭失败：{err}'
    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except OSError:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api='', speed_result=None)


@app.route('/sticky-settings', methods=['POST'])
@login_required
def sticky_settings():
    settings = load_settings()
    sticky = settings.get('sticky', {})
    test_url = request.form.get('test_url', '').strip()
    if test_url:
        sticky['test_url'] = test_url
    try:
        timeout = int(request.form.get('timeout', 600))
        if 60 <= timeout <= 86400:
            sticky['timeout'] = timeout
    except (TypeError, ValueError):
        pass
    sticky['test_enabled'] = request.form.get('test_enabled') == 'on'
    settings['sticky'] = sticky
    save_settings(settings)
    _sync_sticky_from_settings(settings)
    ok, err = _reload_sticky(settings)
    msg = '粘性设置已保存' if ok else f'保存失败：{err}'
    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except OSError:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api='', speed_result=None)


@app.route('/sticky-release', methods=['POST'])
@login_required
def sticky_release():
    task_id = request.form.get('task_id', '').strip()
    s, err = release_session(task_id)
    ok = s is not None
    msg = f'会话 {task_id} 已释放' if ok else f'释放失败：{err}'
    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except OSError:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api='', speed_result=None)


# ==================== API 认证与接口 ====================
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
    status['sticky_enabled'] = STICKY_STATE['enabled']
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


@app.route('/api/session/acquire', methods=['POST'])
def api_session_acquire():
    if not api_authorized():
        return api_auth_error()
    data = _json_body()
    task_id = data.get('task_id') or request.form.get('task_id', '')
    s, err = acquire_session(task_id)
    if s is None:
        return jsonify({'ok': False, 'error': err}), 400
    return jsonify({'ok': True, 'session': _session_public(s)})


@app.route('/api/session/release', methods=['POST'])
def api_session_release():
    if not api_authorized():
        return api_auth_error()
    data = _json_body()
    task_id = data.get('task_id') or request.form.get('task_id', '')
    s, err = release_session(task_id)
    if s is None:
        return jsonify({'ok': False, 'error': err}), 404
    return jsonify({'ok': True, 'message': '会话已释放'})


@app.route('/api/session/status', methods=['GET'])
def api_session_status():
    if not api_authorized():
        return api_auth_error()
    task_id = request.args.get('task_id', '').strip()
    with STICKY_LOCK:
        s = STICKY_STATE['sessions'].get(task_id)
        if not s:
            return jsonify({'ok': False, 'error': '会话不存在'}), 404
        pub = _session_public(s)
    return jsonify({'ok': True, 'session': pub})


@app.route('/api/session/list', methods=['GET'])
def api_session_list():
    if not api_authorized():
        return api_auth_error()
    with STICKY_LOCK:
        sessions = [_session_public(s) for s in STICKY_STATE['sessions'].values()]
    sessions.sort(key=lambda x: x['acquired_at'])
    return jsonify({'ok': True, 'sessions': sessions, 'total': len(sessions)})


@app.route('/api/pool/status', methods=['GET'])
def api_pool_status():
    if not api_authorized():
        return api_auth_error()
    with STICKY_LOCK:
        pool = STICKY_STATE['pool']
        total = len(pool)
        proxies = []
        for k, v in pool.items():
            status = 'busy' if v['in_use'] > 0 else 'available'
            health = 'unhealthy' if not v['available'] else 'healthy'
            proxies.append({'proxy': v['proxy'], 'in_use': v['in_use'], 'status': status, 'health': health})
        available = sum(1 for v in pool.values() if v['available'])
        in_use = sum(v['in_use'] for v in pool.values())
        f_nodes = len(STICKY_STATE['f_nodes'])
        sessions = len(STICKY_STATE['sessions'])
    return jsonify({'ok': True, 'pool': {
        'total': total, 'available': available, 'in_use': in_use, 'queued': 0,
        'sessions': sessions, 'f_nodes': f_nodes, 'proxies': proxies,
        'enabled': STICKY_STATE['enabled'],
    }})


@app.route('/sticky-rotate', methods=['POST'])
@login_required
def sticky_rotate_web():
    task_id = request.form.get('task_id', '').strip()
    s, err = rotate_session(task_id)
    ok = s is not None
    msg = f'会话 {task_id} 已切换' if ok else f'切换失败：{err}'
    status = test_proxy()
    status['mode'] = get_mode()
    try:
        with open(CONFIG_PATH) as f:
            cfg_text = f.read()
    except OSError:
        cfg_text = ''
    return render_page(authed=True, status=status, current_config=cfg_text, message=msg, success=ok,
                       saved_api='', speed_result=None)


# ==================== React 前端 JSON API 层 ====================
def _json_body():
    """安全解析 JSON body：非对象（字符串/数组/null）一律视为空 dict"""
    raw = request.get_json(silent=True)
    return raw if isinstance(raw, dict) else {}


def _login_required_json():
    """API Key 全面鉴权：未配置 Key → 403（禁止管理，仅开放提取）；Key 不匹配 → 401。

    支持 X-API-Key 头、?key= 参数、JSON body 的 key 字段三种传递方式。
    """
    settings = load_settings()
    expected = (settings.get('api_key') or '').strip()
    if not expected:
        return jsonify({'ok': False, 'error': 'key_not_configured',
                        'message': '服务未设置 API Key，管理接口已禁止访问'}), 403
    supplied = request.headers.get('X-API-Key', '') or request.args.get('key', '') or \
        (_json_body()).get('key', '')
    if not secrets.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized', 'message': 'API Key 无效'}), 401
    return None


def _pub_settings(settings):
    """脱敏后的对外设置副本（密码隐藏）。"""
    pub = json.loads(json.dumps(settings, ensure_ascii=False))
    pub.pop('api_key', None)
    for kind in ('socks', 'http'):
        item = pub.get(kind, {})
        if item.get('password'):
            item['password'] = '********'
    saved = pub.get('saved_scenarios', {})
    for sc in saved.values():
        if sc.get('password'):
            sc['password'] = '********'
    return pub


def _conn_urls_for(settings, host=None):
    host = host or request.host.split(':', 1)[0]
    mode_labels = {'mixed': 'SOCKS5/HTTP 混合', 'socks': 'SOCKS5', 'http': 'HTTP', 'dual': 'SOCKS5 + HTTP'}
    out = {}
    for kind in ('socks', 'http'):
        item = settings.get(kind, {})
        scheme = 'socks5' if kind == 'socks' else 'http'
        url = f"{scheme}://{host}:{item.get('port', 7890)}"
        masked = url
        auth = ''
        if item.get('username'):
            auth = f"{item['username']}:********@"
            masked = f"{scheme}://{auth}{host}:{item.get('port', 7890)}"
        out[kind] = {'enabled': item.get('enabled', False), 'host': host, 'port': item.get('port'),
                     'username': item.get('username', ''), 'masked_url': masked, 'url': url}
    return out, mode_labels.get(settings.get('entry_mode', ''), '')


@app.route('/api/ui/login', methods=['POST'])
def api_ui_login():
    err = _login_required_json()
    if err:
        return err
    session['authed'] = True  # 兼容旧模板页面
    return jsonify({'ok': True, 'authed': True})


@app.route('/api/ui/logout', methods=['POST'])
def api_ui_logout():
    session.pop('authed', None)
    return jsonify({'ok': True})


@app.route('/api/ui/bootstrap', methods=['GET'])
def api_ui_bootstrap():
    err = _login_required_json()
    if err:
        return err
    settings = load_settings()
    host = request.host.split(':', 1)[0]
    status = test_proxy()
    status['mode'] = get_mode()
    status['sticky_enabled'] = STICKY_STATE['enabled']
    conn_urls, entry_mode_label = _conn_urls_for(settings, host)
    with STICKY_LOCK:
        sessions = [_session_public(s) for s in STICKY_STATE['sessions'].values()]
        pool = {
            'total': len(STICKY_STATE['pool']),
            'available': sum(1 for v in STICKY_STATE['pool'].values() if v['available']),
            'in_use': sum(v['in_use'] for v in STICKY_STATE['pool'].values()),
        }
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
            for listener in cfg.get('listeners', []):
                for user in listener.get('users', []):
                    if user.get('password'):
                        user['password'] = '********'
            config_text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    except Exception:
        config_text = ''
    return jsonify({
        'ok': True,
        'settings': _pub_settings(settings),
        'api_key': settings.get('api_key', ''),
        'status': status,
        'sticky': {
            'enabled': STICKY_STATE['enabled'],
            'test_url': STICKY_STATE['test_url'],
            'test_enabled': STICKY_STATE['test_enabled'],
            'timeout': STICKY_STATE['timeout'],
        },
        'pool': pool,
        'sessions': sessions,
        'conn_urls': conn_urls,
        'entry_mode_label': entry_mode_label,
        'conn_ready': bool(settings.get('socks', {}).get('username') or settings.get('http', {}).get('username')),
        'config_text': config_text,
        'server': host,
    })


@app.route('/api/ui/action', methods=['POST'])
def api_ui_action():
    err = _login_required_json()
    if err:
        return err
    data = _json_body()
    act = data.get('act') or request.form.get('act')
    msg, ok = '', True
    speed_result = None
    if act == 'restart':
        try:
            get_docker_client().containers.get('mihomo').restart()
            time.sleep(2)
            msg = 'mihomo 已重启'
        except Exception as e:
            msg, ok = f'重启失败：{e}', False
    elif act == 'refresh':
        r = subprocess.run(['bash', REFRESH_SCRIPT], capture_output=True, text=True, timeout=30)
        time.sleep(2)
        msg = '代理已刷新' if r.returncode == 0 else '刷新失败（代理可能还活着）'
        ok = r.returncode == 0
    elif act == 'test':
        e_err = None
        if STICKY_STATE['enabled'] and load_settings().get('scenario') == 'E':
            _, e_err = _ensure_e_proxy()
        status = test_proxy()
        if not status['alive']:
            time.sleep(1)
            status = test_proxy()
        status['mode'] = get_mode()
        if status['alive']:
            msg = f'代理正常，出口 IP: {status["ip"]} ({status["country"]})'
        else:
            msg, ok = f'代理不可用{e_err or ""}', False
        return jsonify({'ok': ok, 'message': msg, 'status': status})
    elif act == 'speed':
        e_err = None
        if STICKY_STATE['enabled'] and load_settings().get('scenario') == 'E':
            with STICKY_LOCK:
                has_node = bool(STICKY_STATE['e_test_nodes']) or any(
                    s.get('scenario') == 'E' and s.get('proxy_node')
                    for s in STICKY_STATE['sessions'].values())
            if not has_node:
                _, e_err = _ensure_e_proxy(need_speed=True)
        if e_err:
            msg, ok = f'测速失败：{e_err}', False
        else:
            speed_result, msg = _speed_download(12)
            ok = speed_result is not None
            if not ok and STICKY_STATE['enabled'] and load_settings().get('scenario') == 'E':
                with STICKY_LOCK:
                    STICKY_STATE['e_test_nodes'] = []
                _, e_err = _ensure_e_proxy(need_speed=True)
                if not e_err:
                    speed_result, msg = _speed_download(12)
                    ok = speed_result is not None
        return jsonify({'ok': ok, 'message': msg, 'speed': speed_result})
    status = test_proxy()
    status['mode'] = get_mode()
    return jsonify({'ok': ok, 'message': msg, 'status': status})


def _exec_apply(data):
    """场景保存/切换核心逻辑，供 UI 与 /api/v1/config 复用。data 为合并后的字段字典。"""
    scenario = data.get('scenario')
    is_switch = data.get('switch') == '1'
    settings = load_settings()
    saved = settings.get('saved_scenarios', {})
    msg, ok = '', False
    if is_switch:
        params = saved.get(scenario, {})
        if not params and scenario != 'A':
            msg = '没有已保存的配置，请先保存应用'
        elif scenario == 'A':
            params = {'configured': True}
    else:
        params = {}
        if scenario == 'proxy':
            params = {
                'proxy_type': data.get('proxy_type', 'socks5'),
                'proxies': (data.get('proxies') or '').strip(),
                'username': (data.get('username') or '').strip(),
                'password': (data.get('password') or '').strip(),
                'rotate': data.get('rotate', 'yes'),
            }
        elif scenario == 'E':
            params = {
                'api_url': (data.get('api_url') or '').strip(),
                'api_num': data.get('api_num', '1'),
            }
        elif scenario == 'F':
            params = {
                'clash_url': (data.get('clash_url') or '').strip(),
                'mode': data.get('f_mode', 'direct'),
            }
        elif scenario == 'A':
            params = {'configured': True}
        saved[scenario] = params
        settings['saved_scenarios'] = saved

    if not msg:
        if STICKY_STATE['enabled']:
            ok, msg = True, '场景参数已保存（粘性模式已开启）'
        elif scenario == 'A':
            cfg = gen_direct_config()
            ok, err = deploy_mihomo(cfg)
            msg = '已切换到直连模式' if ok else f'失败：{err}'
            subprocess.run('bash -c \'crontab -l 2>/dev/null | grep -v cliproxy_refresh | crontab -\'', shell=True, capture_output=True)
        elif scenario == 'proxy':
            ptype = params.get('proxy_type', 'socks5')
            proxies = params.get('proxies', '').split('\n')
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

    if ok:
        settings['scenario'] = scenario
        save_settings(settings)
        if STICKY_STATE['enabled']:
            _clear_sessions()
            _sync_sticky_from_settings(settings)
            if scenario == 'proxy':
                _rebuild_pool_from_settings(settings)
            elif scenario == 'F':
                _refresh_f_nodes(settings)
            if scenario != 'E':
                with STICKY_LOCK:
                    STICKY_STATE['e_test_nodes'] = []
            ok2, err2 = _reload_sticky(settings)
            if not ok2:
                msg, ok = f'粘性配置应用失败：{err2}', False
            else:
                msg = '场景已切换（粘性模式），已重建配置'
                if scenario == 'E':
                    _, e_err = _ensure_e_proxy(settings)
                    msg = '场景已切换（粘性模式），已自动提取测试节点' if not e_err else f'场景已切换，但测试节点提取失败：{e_err}'
    time.sleep(1)
    return {'ok': ok, 'message': msg}


@app.route('/api/ui/apply', methods=['POST'])
def api_ui_apply():
    err = _login_required_json()
    if err:
        return err
    data = {**request.form, **(_json_body())}
    return jsonify(_exec_apply(data))


def _exec_terminal(data):
    """对外连接设置/API Key 保存核心逻辑，供 UI 与 /api/v1/config 复用。data 为合并后的字段字典。"""
    old = load_settings()
    new_key = (data.get('api_key') or '').strip()
    # 仅更新 API Key：与入口配置无关，独立保存，跳过入口校验/部署
    key_only = bool(new_key) and not any(k in data for k in (
        'entry_mode', 'entry_port', 'entry_username', 'entry_password',
        'socks_enabled', 'socks_port', 'socks_username', 'socks_password',
        'http_enabled', 'http_port', 'http_username', 'http_password',
        'exit_mode'))
    if key_only:
        if new_key == old.get('api_key', ''):
            return {'ok': True, 'message': 'API Key 未变化'}
        old['api_key'] = new_key
        save_settings(old)
        return {'ok': True, 'message': 'API Key 已更新'}
    exit_mode = data.get('exit_mode') or old.get('exit_mode', 'scenario')
    entry_mode = data.get('entry_mode') or old.get('entry_mode', 'dual')
    settings = {
        'scenario': old['scenario'],
        'saved_scenarios': old.get('saved_scenarios', {}),
        'sticky': old.get('sticky', {}),
        'api_key': (data.get('api_key') or '').strip() or old['api_key'],
        'entry_mode': entry_mode, 'exit_mode': exit_mode,
    }
    socks, http = dict(old['socks']), dict(old['http'])
    if entry_mode in ('mixed', 'socks', 'http'):
        # 部分更新语义：未提供的字段保留旧值
        old_item = old['http'] if entry_mode == 'http' else old['socks']
        port = (data.get('entry_port') or '').strip() or (old_item.get('port') or '')
        username = (data.get('entry_username') or '').strip() or (old_item.get('username') or '')
        password = data.get('entry_password') or old_item.get('password', '')
        if entry_mode == 'http':
            http.update(enabled=True, port=port, username=username, password=password)
            socks.update(enabled=False)
        else:
            socks.update(enabled=True, port=port, username=username, password=password)
            http.update(enabled=False)
    else:
        for kind in ('socks', 'http'):
            entered_password = data.get(f'{kind}_password') or ''
            item = dict(old[kind])
            item.update(
                enabled=data.get(f'{kind}_enabled') == 'on' if f'{kind}_enabled' in data else item['enabled'],
                port=(data.get(f'{kind}_port') or '').strip() or item.get('port') or '',
                username=(data.get(f'{kind}_username') or '').strip() or item.get('username', ''),
                password=entered_password if entered_password else item['password'],
            )
            if not item['username']:
                item['password'] = ''
            (socks if kind == 'socks' else http).update(item)
    settings['socks'], settings['http'] = socks, http
    error = validate_terminal_settings(settings)
    msg, ok = '', False
    if error:
        msg = error
    else:
        if STICKY_STATE['enabled']:
            ok, deploy_error = _reload_sticky(settings)
            if ok:
                save_settings(settings)
                msg = '对外连接设置已保存并应用（粘性模式）'
            else:
                msg = f'应用失败：{deploy_error}'
        else:
            if exit_mode == 'direct':
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
                msg = '对外连接设置已保存并应用'
            else:
                msg = f'应用失败：{deploy_error}'
    return {'ok': ok, 'message': msg}


@app.route('/api/ui/terminal', methods=['POST'])
def api_ui_terminal():
    err = _login_required_json()
    if err:
        return err
    data = {**request.form, **(_json_body())}
    return jsonify(_exec_terminal(data))


@app.route('/api/ui/sticky-toggle', methods=['POST'])
def api_ui_sticky_toggle():
    err = _login_required_json()
    if err:
        return err
    settings = load_settings()
    sticky = settings.get('sticky', {})
    sticky['enabled'] = not sticky.get('enabled', False)
    settings['sticky'] = sticky
    save_settings(settings)
    _sync_sticky_from_settings(settings)
    msg, ok = '', True
    if sticky['enabled']:
        subprocess.run('bash -c \'crontab -l 2>/dev/null | grep -v cliproxy_refresh | crontab -\'', shell=True, capture_output=True)
        scenario = settings['scenario']
        if scenario == 'proxy':
            _rebuild_pool_from_settings(settings)
        elif scenario == 'F':
            _refresh_f_nodes(settings)
        ok2, err = _reload_sticky(settings)
        if not ok2:
            msg, ok = f'粘性模式开启失败：{err}', False
        else:
            msg = '粘性会话模式已开启'
    else:
        _clear_sessions('关闭粘性模式')
        if settings['scenario'] == 'E':
            cfg = gen_api_config(settings['saved_scenarios'].get('E', {}).get('api_url', ''), '1')
            ok, err = deploy_mihomo(cfg, scenario_e=True, settings=settings)
        else:
            current = gen_direct_config()
            if settings['scenario'] == 'proxy':
                p = settings['saved_scenarios'].get('proxy', {})
                cfg, err = gen_proxy_config(p.get('proxy_type', 'socks5'), p.get('proxies', '').split('\n'),
                                            p.get('username', ''), p.get('password', ''), p.get('rotate', 'yes'))
                current = cfg
            elif settings['scenario'] == 'F':
                current = gen_subscription_config(settings['saved_scenarios'].get('F', {}).get('clash_url', ''))
            ok, err = deploy_mihomo(current, settings=settings)
        msg = '粘性会话模式已关闭，恢复原场景' if ok else f'关闭失败：{err}'
    return jsonify({'ok': ok, 'message': msg})


@app.route('/api/ui/sticky-settings', methods=['POST'])
def api_ui_sticky_settings():
    err = _login_required_json()
    if err:
        return err
    data = _json_body()
    settings = load_settings()
    sticky = settings.get('sticky', {})
    test_url = (data.get('test_url') or request.form.get('test_url', '')).strip()
    if test_url:
        sticky['test_url'] = test_url
    try:
        timeout = int(data.get('timeout') or request.form.get('timeout', 600))
        if 60 <= timeout <= 86400:
            sticky['timeout'] = timeout
    except (TypeError, ValueError):
        pass
    sticky['test_enabled'] = (data.get('test_enabled', '') or request.form.get('test_enabled')) == 'on' or bool(data.get('test_enabled') is True)
    settings['sticky'] = sticky
    save_settings(settings)
    _sync_sticky_from_settings(settings)
    ok, err = _reload_sticky(settings)
    msg = '粘性设置已保存' if ok else f'保存失败：{err}'
    return jsonify({'ok': ok, 'message': msg})


@app.route('/api/ui/sticky-release', methods=['POST'])
def api_ui_sticky_release():
    err = _login_required_json()
    if err:
        return err
    data = _json_body()
    task_id = (data.get('task_id') or request.form.get('task_id', '')).strip()
    s, err = release_session(task_id)
    ok = s is not None
    return jsonify({'ok': ok, 'message': f'会话 {task_id} 已释放' if ok else f'释放失败：{err}'})


@app.route('/api/ui/sticky-rotate', methods=['POST'])
def api_ui_sticky_rotate():
    err = _login_required_json()
    if err:
        return err
    data = _json_body()
    task_id = (data.get('task_id') or request.form.get('task_id', '')).strip()
    s, err = rotate_session(task_id)
    ok = s is not None
    return jsonify({'ok': ok, 'message': f'会话 {task_id} 已切换' if ok else f'切换失败：{err}'})


# 统一取代理 API（仿 Relay-Scout /api/v1/proxy 风格）：一个接口拿 IP
# GET /api/v1/proxy?key=K&session=会话ID&consume=1&format=txt
#   - 无 session：返回共享出口信息（socks/http URL）
#   - 有 session：粘性会话 acquire，返回独立端口 listener（IP 独占），同 session 幂等
#   - consume=1：释放该 session（仿消耗式提取，取走即退）
@app.route('/api/v1/config', methods=['GET', 'POST'])
def api_v1_config():
    """统一配置接口（管理面）：GET 查看对外连接配置；POST 控制入口 / API Key / 场景。"""
    err = _login_required_json()
    if err:
        return err
    host = request.host.split(':', 1)[0]
    if request.method == 'GET':
        settings = load_settings()
        conn, mode_label = _conn_urls_for(settings, host)
        return jsonify({
            'ok': True,
            'entry_mode': settings.get('entry_mode', 'dual'),
            'entry_mode_label': mode_label,
            'exit_mode': settings.get('exit_mode', 'scenario'),
            'scenario': settings.get('scenario'),
            'connections': conn,
            'sticky_enabled': STICKY_STATE['enabled'],
            'conn_ready': bool(settings.get('socks', {}).get('username') or settings.get('http', {}).get('username')),
            'server': host,
        })
    data = {**request.form, **(_json_body())}
    if 'scenario' in data or 'switch' in data:
        return jsonify(_exec_apply(data))
    if any(k in data for k in (
            'entry_mode', 'entry_port', 'entry_username', 'entry_password',
            'socks_enabled', 'socks_port', 'socks_username', 'socks_password',
            'http_enabled', 'http_port', 'http_username', 'http_password',
            'exit_mode', 'api_key')):
        return jsonify(_exec_terminal(data))
    return jsonify({'ok': False, 'error': 'BAD_REQUEST',
                    'message': '缺少可操作的配置字段，请参考 API 文档'}), 400


@app.route('/api/v1/proxy', methods=['GET', 'POST'])
def api_v1_proxy():
    settings0 = load_settings()
    supplied = request.args.get('key', '') or request.headers.get('X-API-Key', '') or \
        (_json_body()).get('key', '')
    expected = (settings0.get('api_key') or '').strip()
    if expected and not secrets.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    host = request.host.split(':', 1)[0]
    kwargs = _json_body()
    fmt = request.args.get('format') or kwargs.get('format', 'json')
    session_id = (request.args.get('session') or kwargs.get('session', '')).strip()
    consume = request.args.get('consume') or kwargs.get('consume', '')
    if consume == '1' or consume is True:
        # 消耗式：释放该 session 绑定的端口（IP 交还池）
        if not session_id:
            return jsonify({'ok': False, 'error': 'SESSION_REQUIRED', 'message': 'consume=1 需要带 session'}), 400
        s, err = release_session(session_id)
        if s is None:
            return jsonify({'ok': False, 'error': 'SESSION_NOT_FOUND', 'message': f'会话不存在或已释放：{err}'}), 404
        data = {'ok': True, 'consumed': True, 'session': session_id}
        return (f"ok {session_id}\n" if fmt == 'txt' else jsonify(data))
    if session_id:
        # 粘性会话：acquire，返回独立端口 listener
        s, err = acquire_session(session_id)
        if s is None:
            if err == '粘性会话模式未开启':
                return jsonify({'ok': False, 'error': 'STICKY_DISABLED', 'message': err}), 400
            if err and '端口已用尽' in err:
                return jsonify({'ok': False, 'error': 'POOL_EXHAUSTED', 'message': err}), 409
            return jsonify({'ok': False, 'error': 'SESSION_ACQUIRE_FAILED', 'message': err}), 400
        listener = f"{host}:{s['port']}"
        scheme_user = ''
        st = settings0.get('sticky', {})
        u = st.get('username', '') or settings0.get('socks', {}).get('username', '')
        p = st.get('password', '') or settings0.get('socks', {}).get('password', '')
        if u:
            scheme_user = f"{quote(u, safe='')}:{quote(p, safe='')}@"
        url = f"socks5://{scheme_user}{host}:{s['port']}"
        data = {'ok': True, 'session': s['task_id'], 'proxy': {'proxy': url, 'ip': host, 'port': s['port']},
                'sticky': {'bound': True, 'expires_in': int(s['expires'] - time.time()) if s.get('expires') else None}}
        return (f"{url}\n" if fmt == 'txt' else jsonify(data))
    # 无 session：返回共享出口信息
    conn, _ = _conn_urls_for(settings0, host)
    data = {'ok': True, 'connections': conn}
    if fmt == 'txt':
        url = conn.get('socks', {}).get('url') or conn.get('http', {}).get('url', '')
        return f"{url}\n"
    return jsonify(data)


@app.route('/api/v1/proxy/destroy', methods=['GET', 'POST'])
def api_v1_proxy_destroy():
    settings0 = load_settings()
    supplied = request.args.get('key', '') or request.headers.get('X-API-Key', '') or \
        (_json_body()).get('key', '')
    expected = (settings0.get('api_key') or '').strip()
    if expected and not secrets.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    kwargs = _json_body()
    session_id = (request.args.get('session') or kwargs.get('session', '')).strip()
    if not session_id:
        return jsonify({'ok': False, 'error': 'SESSION_REQUIRED', 'message': '销毁必须带 session 参数'}), 400
    s, err = release_session(session_id)
    if s is None:
        return jsonify({'ok': False, 'error': 'SESSION_NOT_FOUND', 'message': f'会话不存在或已释放：{err}'}), 404
    return jsonify({'ok': True, 'destroyed': True, 'session': session_id})


HTML = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mihomo Relay WebUI - Mihomo 代理中转管理终端</title>
<style>
:root{
  --bg:#f4f6fb;--panel:#ffffff;--panel2:#fbfcfe;--line:#e3e8f0;
  --txt:#1c2739;--mut:#5b6a84;--dim:#8b96ab;
  --acc:#2f6bff;--acc2:#4d8dff;--grn:#16a34a;--red:#dc2626;--amb:#b45309;--cyn:#0369a1;
  --grad:linear-gradient(135deg,#2f6bff,#4d8dff);
  --sh:0 1px 3px rgba(16,24,40,.06);
}
*{box-sizing:border-box}
html,body{margin:0}
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;padding:0 16px 48px}
.wrap{max-width:940px;margin:0 auto}
header{display:flex;align-items:center;gap:14px;padding:26px 4px 6px;flex-wrap:wrap}
.logo{width:42px;height:42px;border-radius:10px;background:var(--acc);display:grid;place-items:center;font-weight:800;font-size:15px;color:#fff;letter-spacing:.5px}
header h1{font-size:19px;margin:0;font-weight:700}
header .sub{font-size:12px;color:var(--mut);margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--line);background:var(--panel);color:var(--mut)}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}
.pill.on{color:var(--grn);border-color:rgba(22,163,74,.35);background:rgba(22,163,74,.08)}
.pill.on .dot{background:var(--grn)}
.pill.off{color:var(--red);border-color:rgba(220,38,38,.35);background:rgba(220,38,38,.08)}
.pill.off .dot{background:var(--red)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0;box-shadow:var(--sh)}
.card h2{margin:0 0 14px;font-size:15px;font-weight:700;display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none}
.card h2 .tag{display:inline-flex;align-items:center;gap:10px;min-width:0}
.card h2 .ico{width:26px;height:26px;border-radius:7px;background:var(--acc);display:inline-grid;place-items:center;font-size:12px;font-weight:800;color:#fff;flex:none}
.card h2 .arr{width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-top:6px solid var(--mut);transition:transform .25s;flex:none}
.card.collapsed .arr{transform:rotate(-90deg)}
.card.collapsed .content{display:none}
.card h2 .hint{font-size:11px;color:var(--dim);font-weight:400;margin-left:auto;padding-right:8px}
.content{margin-top:4px}
label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px;letter-spacing:.3px}
input,select,textarea{width:100%;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--txt);font-size:13.5px;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(47,107,255,.12)}
textarea{font-family:ui-monospace,Consolas,monospace;resize:vertical}
button{padding:8px 18px;border:none;border-radius:8px;background:var(--acc);color:#fff;cursor:pointer;font-size:13.5px;font-weight:600;margin-top:10px;transition:filter .15s}
button:hover{filter:brightness(1.08)}
button:active{transform:translateY(1px)}
.btn-red{background:#dc2626}
.btn-blue{background:#2f6bff}
.btn-small{padding:4px 12px;font-size:12px;margin:2px;border-radius:7px}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>div,.row>form{flex:1;min-width:150px}
.status{padding:10px 14px;border-radius:8px;margin:10px 0;font-size:13.5px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.status.ok{background:rgba(22,163,74,.08);border:1px solid rgba(22,163,74,.3);color:var(--grn)}
.status.err{background:rgba(220,38,38,.08);border:1px solid rgba(220,38,38,.3);color:var(--red)}
pre{background:#f8fafc;border:1px solid var(--line);padding:12px;border-radius:8px;overflow:auto;font-size:12px;max-height:400px;color:#3b4a63}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.note{font-size:12.5px;color:var(--mut);margin:10px 0;padding:10px 14px;background:rgba(180,83,9,.06);border:1px solid rgba(180,83,9,.2);border-left:3px solid var(--amb);border-radius:8px;line-height:1.7}
.note b{color:var(--txt)}
.note.warning{background:rgba(220,38,38,.06);border-color:rgba(220,38,38,.25);border-left-color:var(--red)}
.terminal-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.terminal-card{border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0;background:var(--panel2)}
.terminal-card h3{margin:0 0 10px;font-size:14px;display:flex;align-items:center;gap:6px}
.inline-check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--txt)}
.inline-check input{width:auto}
.secret-wrap{display:flex;gap:6px}.secret-wrap input{flex:1}.secret-wrap button{margin:0;padding:6px 12px}
.mini-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.mini-actions button{margin:0;padding:6px 12px}
.copy-value{font:12px ui-monospace,Consolas,monospace;overflow-wrap:anywhere;color:var(--cyn);background:rgba(3,105,161,.06);border:1px dashed rgba(3,105,161,.3);border-radius:6px;padding:4px 8px;display:inline-block;max-width:100%}
.tabs{display:flex;gap:6px;margin:16px 0 4px;flex-wrap:wrap}
.tab{padding:8px 18px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--mut);cursor:pointer;font-size:13.5px;font-weight:600;margin:0;box-shadow:none}
.tab:hover{color:var(--txt)}
.tab.active{background:var(--acc);color:#fff;border-color:transparent}
.steps{display:flex;flex-direction:column;gap:10px}
.step{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}
.step .n{flex:none;width:26px;height:26px;border-radius:50%;background:var(--panel);border:1px solid var(--line);display:grid;place-items:center;font-size:12px;font-weight:700;color:var(--mut)}
.step.done .n{background:var(--grn);border-color:transparent;color:#fff}
.step.done{opacity:.8}
.step b{color:var(--txt)}
.step .d{font-size:12.5px;color:var(--mut);line-height:1.7}
.qs-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
.qs-row .copy-value{flex:1;min-width:220px}
.port-table td:first-child{font-family:ui-monospace,Consolas,monospace;color:var(--cyn)}
.api-card h3{margin:16px 0 6px;font-size:14px;display:flex;align-items:center;gap:8px}
.api-card h3 .m{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;background:rgba(47,107,255,.1);color:var(--acc)}
.api-card .path{font:12px ui-monospace,Consolas,monospace;color:var(--amb)}
.api-card .params{font-size:12.5px;color:var(--mut);line-height:1.8}
.api-card .params code{color:var(--cyn)}
.cmd-row{display:flex;gap:8px;align-items:center;margin-top:8px}
.cmd-row pre{flex:1;margin:0}
.cmd-row button{margin:0;padding:6px 14px}
.toast{display:none;position:fixed;right:20px;bottom:20px;background:#1f2937;color:#fff;padding:10px 16px;border-radius:8px;z-index:10;border:1px solid var(--line);box-shadow:0 8px 24px rgba(16,24,40,.18);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;border-radius:8px;overflow:hidden}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
th{background:rgba(47,107,255,.06);color:var(--mut);font-weight:600;font-size:12px;letter-spacing:.4px}
tbody tr:hover{background:rgba(47,107,255,.04)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat .k{font-size:11.5px;color:var(--mut);letter-spacing:.5px}
.stat .v{font-size:20px;font-weight:800;margin-top:4px}
.stat .v.grn{color:var(--grn)}.stat .v.red{color:var(--red)}.stat .v.acc{color:var(--acc)}.stat .v.amb{color:var(--amb)}
.cur{font-size:11px;color:var(--grn);font-weight:600;background:rgba(22,163,74,.1);border:1px solid rgba(22,163,74,.3);padding:2px 8px;border-radius:999px;margin-left:6px;white-space:nowrap}
code{background:rgba(47,107,255,.1);color:var(--acc);padding:1px 6px;border-radius:5px;font-size:12px}
@media(max-width:720px){.terminal-grid,.grid2{grid-template-columns:1fr}.row{flex-direction:column}.row>div,.row>form{flex:auto;min-width:0}.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="wrap">
<header>
<div class="logo">MR</div>
<div>
<h1>Mihomo Relay WebUI</h1>
<div class="sub">Mihomo 代理中转管理终端 · 粘性会话</div>
</div>
{% if authed %}
<div style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap">
<span class="pill {{ 'on' if sticky.enabled else 'off' }}"><span class="dot"></span>粘性会话 {{ '已开启' if sticky.enabled else '已关闭' }}</span>
<span class="pill {{ 'on' if status.alive else 'off' }}"><span class="dot"></span>代理 {{ '正常' if status.alive else '异常' }}</span>
</div>
{% endif %}
</header>

{% if not authed %}
<div class="card" style="max-width:400px;margin:60px auto;text-align:center">
<h2 style="justify-content:center"><span class="tag">登录管理面板</span><span class="arr"></span></h2>
<form method="post" action="/login">
<input type="password" name="pwd" placeholder="管理密码" style="text-align:center">
<button type="submit" style="width:100%">进入</button>
</form>
</div>
{% else %}

<nav class="tabs" id="mainTabs">
<button type="button" class="tab active" data-tab="overview" onclick="switchTab('overview')">概览</button>
<button type="button" class="tab" data-tab="config" onclick="switchTab('config')">连接配置</button>
<button type="button" class="tab" data-tab="sticky" onclick="switchTab('sticky')">粘性会话</button>
<button type="button" class="tab" data-tab="api" onclick="switchTab('api')">API 文档</button>
</nav>

<div class="card" data-tab="overview">
<h2><span class="tag"><span class="ico">状</span>当前状态</span><span class="arr"></span></h2>
<div class="content">
<div class="stats">
<div class="stat"><div class="k">代理状态</div><div class="v {{ 'grn' if status.alive else 'red' }}">{{ '正常' if status.alive else '不可用' }}</div></div>
{% if status.ip %}<div class="stat"><div class="k">出口 IP</div><div class="v acc" style="font-size:15px">{{ status.ip }}{% if status.country %}（{{ status.country }}）{% endif %}</div></div>{% endif %}
{% if status.mode %}<div class="stat"><div class="k">运行模式</div><div class="v amb" style="font-size:16px">{{ status.mode }}</div></div>{% endif %}
<div class="stat"><div class="k">粘性会话</div><div class="v {{ 'grn' if sticky.enabled else 'red' }}">{{ '已开启' if sticky.enabled else '已关闭' }}</div></div>
</div>
<div class="row" style="margin-top:12px">
<button type="button" class="btn-blue" onclick="runAction('test', this)">测试代理</button>
<button type="button" class="btn-blue" onclick="runAction('speed', this)">测速</button>
<form method="post" action="/action" style="flex:1"><input type="hidden" name="act" value="restart"><button type="submit">重启 mihomo</button></form>
</div>
</div>
</div>

<div class="card" data-tab="overview">
<h2><span class="tag"><span class="ico">指</span>快速开始</span><span class="arr"></span></h2>
<div class="content">
<div class="steps">
<div class="step {{ 'done' if settings.socks.username or settings.http.username }}">
<span class="n">1</span>
<div class="d"><b>设置对外入口账号密码</b> — 到「连接配置」的<code>对外连接</code>填写入口用户名/密码并保存，这是客户端连接时的认证凭据。</div>
</div>
<div class="step {{ 'done' if settings.scenario in ('A','proxy','E','F') }}">
<span class="n">2</span>
<div class="d"><b>选择场景并保存应用</b> — 按你的上游来源选一个场景（挂代理 / API 提取 / 订阅 / 直连），点「保存应用」生效。</div>
</div>
<div class="step {{ 'done' if conn_ready }}">
<span class="n">3</span>
<div class="d"><b>复制连接链接使用</b> — 用下面的链接接入，或用「API 文档」页签的接口做程序化接入。</div>
</div>
</div>
<div class="qs-row">
<p class="copy-value" id="qsSocksLink">{{ conn_urls.socks_masked }}</p>
<button type="button" class="btn-blue btn-small" onclick="copyQSLink('socks')">复制 SOCKS5 链接</button>
<button type="button" class="btn-blue btn-small" onclick="copyQSLink('http')">复制 HTTP 链接</button>
</div>
<div class="note" style="margin-top:10px">密码含特殊字符（如 <code>@</code>）已自动 URL 编码，复制后直接可用。若入口账号密码未设置，链接将不包含认证信息。</div>
</div>
</div>

<div class="card" data-tab="overview">
<h2><span class="tag"><span class="ico">端</span>端口速查</span><span class="arr"></span></h2>
<div class="content">
<table class="port-table">
<tr><th>端口</th><th>用途</th><th>当前状态</th></tr>
<tr><td>7890</td><td>对外入口（{{ settings.entry_mode_label }}）</td><td>{{ '已开启' if settings.socks.enabled else '未启用' }}</td></tr>
{% if settings.entry_mode == 'dual' %}<tr><td>7891</td><td>HTTP 对外入口</td><td>{{ '已开启' if settings.http.enabled else '未启用' }}</td></tr>{% endif %}
<tr><td>7892</td><td>WebUI 管理面板 + API 接口</td><td>固定</td></tr>
<tr><td>40001-40999</td><td>粘性会话动态端口（{{ '已开启' if sticky.enabled else '未开启' }}）</td><td>{{ '按需分配' if sticky.enabled else '—' }}</td></tr>
</table>
<div class="note" style="margin-top:8px">端口需在云安全组与服务器防火墙同时放行。详见「API 文档」页签或 README。</div>
</div>
</div>

<div class="card" data-tab="sticky">
<h2><span class="tag"><span class="ico">S</span>粘性会话模式</span><span class="hint">端口 40001-40999</span><span class="arr"></span></h2>
<div class="content">
<div class="row">
<form method="post" action="/sticky-toggle" style="flex:1">
<button type="submit" class="{{ 'btn-red' if sticky.enabled else 'btn-blue' }}">{{ '关闭粘性会话模式' if sticky.enabled else '开启粘性会话模式' }}</button>
</form>
</div>
<form method="post" action="/sticky-settings" style="margin-top:8px">
<div class="grid2">
<div><label>测试 URL</label><input type="text" name="test_url" value="{{ sticky.test_url }}"></div>
<div><label>会话超时（秒，E/F轮询）</label><input type="number" name="timeout" value="{{ sticky.timeout }}" min="60"></div>
</div>
<label class="inline-check"><input type="checkbox" name="test_enabled" {% if sticky.test_enabled %}checked{% endif %}> 启用代理可用性验证</label>
<button type="submit" class="btn-blue">保存粘性设置</button>
</form>
{% if sticky.enabled %}
<div class="stats" style="margin-top:12px">
<div class="stat"><div class="k">代理池总数</div><div class="v acc">{{ pool.total }}</div></div>
<div class="stat"><div class="k">可用节点</div><div class="v grn">{{ pool.available }}</div></div>
<div class="stat"><div class="k">占用节点</div><div class="v amb">{{ pool.in_use }}</div></div>
<div class="stat"><div class="k">活跃会话</div><div class="v">{{ sessions|length }}</div></div>
</div>
{% if sessions %}
<table style="margin-top:12px">
<tr><th>task_id</th><th>绑定代理</th><th>端口</th><th>场景</th><th>获取时间</th><th>过期时间</th><th>操作</th></tr>
{% for s in sessions %}
<tr>
<td>{{ s.task_id }}</td><td>{{ s.proxy }}</td><td>{{ s.listener_port }}</td><td>{{ s.scenario }}</td>
<td>{{ s.acquired_at }}</td><td>{{ s.expires_at or '不过期' }}</td>
<td>
<form method="post" action="/sticky-release" style="display:inline"><input type="hidden" name="task_id" value="{{ s.task_id }}"><button type="submit" class="btn-red btn-small">释放</button></form>
<form method="post" action="/sticky-rotate" style="display:inline"><input type="hidden" name="task_id" value="{{ s.task_id }}"><button type="submit" class="btn-blue btn-small">切换</button></form>
</td>
</tr>
{% endfor %}
</table>
{% endif %}
<div class="note" style="margin-top:8px">粘性端口范围 40001-40999，使用 SOCKS 账号密码认证：<code>socks5://用户名:密码@{{ public_host }}:端口</code></div>
{% endif %}
</div>
</div>

<div class="card" data-tab="config">
<h2><span class="tag"><span class="ico">连</span>对外连接</span><span class="arr"></span></h2>
<div class="content">
<form method="post" action="/terminal-settings" id="terminalForm">
<div class="terminal-grid">
<div class="terminal-card" style="grid-column:1/-1">
<h3>入口与出口</h3>
<div class="row" style="gap:10px">
<div style="flex:1">
<label>入口模式（客户端连接方式）</label>
<select name="entry_mode" id="entryMode" onchange="toggleEntryMode()">
<option value="mixed" {% if settings.entry_mode == 'mixed' %}selected{% endif %}>混合单端口（SOCKS5 / HTTP 通用）</option>
<option value="socks" {% if settings.entry_mode == 'socks' %}selected{% endif %}>仅 SOCKS5</option>
<option value="http" {% if settings.entry_mode == 'http' %}selected{% endif %}>仅 HTTP</option>
<option value="dual" {% if settings.entry_mode == 'dual' %}selected{% endif %}>双入口（SOCKS5 + HTTP 独立）</option>
</select>
</div>
<div style="flex:1">
<label>出口模式（流量走向）</label>
<select name="exit_mode" id="exitMode">
<option value="scenario" {% if settings.exit_mode != 'direct' %}selected{% endif %}>跟随场景（A/P/E/F 上游代理）</option>
<option value="direct" {% if settings.exit_mode == 'direct' %}selected{% endif %}>强制直连（服务器 IP 出口）</option>
</select>
</div>
</div>
<div class="note" style="margin-top:6px">入口 = 客户端连进来的方式；出口 = 流量实际走哪。两者独立，切换场景不影响对外链接。</div>
</div>
<div class="terminal-card" id="uniCard" {% if settings.entry_mode == 'dual' %}style="display:none"{% endif %}>
<h3 id="uniTitle">{% if settings.entry_mode == 'http' %}HTTP 入口{% elif settings.entry_mode == 'socks' %}SOCKS5 入口{% else %}混合单端口{% endif %}</h3>
<label>监听端口</label>
<input type="number" name="entry_port" id="entryPort" min="1" max="65535" value="{% if settings.entry_mode == 'http' %}{{ settings.http.port }}{% else %}{{ settings.socks.port }}{% endif %}">
<label>用户名</label>
<input type="text" name="entry_username" id="entryUser" value="{% if settings.entry_mode == 'http' %}{{ settings.http.username }}{% else %}{{ settings.socks.username }}{% endif %}" autocomplete="off">
<label>密码（留空表示保留已保存值）</label>
<div class="secret-wrap"><input type="password" name="entry_password" id="entryPass" placeholder="已保存" autocomplete="new-password"><button type="button" class="btn-blue" onclick="toggleSecret(this)">显示</button></div>
<p class="copy-value" id="uniLink"></p>
<div class="mini-actions">
<button type="button" class="btn-blue" onclick="copyUni('socks5')">复制 SOCKS5</button>
<button type="button" class="btn-blue" onclick="copyUni('http')">复制 HTTP</button>
<button type="submit" name="test_kind" value="main" class="btn-blue">保存并测试</button>
</div>
</div>
<div class="terminal-card" id="dualCard" {% if settings.entry_mode != 'dual' %}style="display:none"{% endif %}>
<h3>SOCKS5</h3>
<label class="inline-check"><input type="checkbox" name="socks_enabled" {% if settings.socks.enabled %}checked{% endif %}> 启用入口</label>
<label>监听端口</label><input type="number" name="socks_port" min="1" max="65535" value="{{ settings.socks.port }}">
<label>用户名</label><input type="text" name="socks_username" value="{{ settings.socks.username }}" autocomplete="off">
<label>密码（留空表示保留已保存值）</label>
<div class="secret-wrap"><input type="password" name="socks_password" placeholder="已保存" autocomplete="new-password"><button type="button" class="btn-blue" onclick="toggleSecret(this)">显示</button></div>
<div class="mini-actions"><button type="button" class="btn-blue" onclick="copyConnection('socks')">复制链接</button></div>
</div>
<div class="terminal-card" id="dualCard2" {% if settings.entry_mode != 'dual' %}style="display:none"{% endif %}>
<h3>HTTP</h3>
<label class="inline-check"><input type="checkbox" name="http_enabled" {% if settings.http.enabled %}checked{% endif %}> 启用入口</label>
<label>监听端口</label><input type="number" name="http_port" min="1" max="65535" value="{{ settings.http.port }}">
<label>用户名</label><input type="text" name="http_username" value="{{ settings.http.username }}" autocomplete="off">
<label>密码（留空表示保留已保存值）</label>
<div class="secret-wrap"><input type="password" name="http_password" placeholder="已保存" autocomplete="new-password"><button type="button" class="btn-blue" onclick="toggleSecret(this)">显示</button></div>
<div class="mini-actions"><button type="button" class="btn-blue" onclick="copyConnection('http')">复制链接</button></div>
</div>
<div class="terminal-card">
<h3>API 控制接口</h3>
<label>监听地址</label><p class="copy-value">http://{{ public_host }}:7892</p>
<label>API Key</label><div class="secret-wrap"><input type="password" name="api_key" value="" placeholder="已保存，留空保留" autocomplete="new-password"><button type="button" class="btn-blue" onclick="toggleSecret(this)">显示</button></div>
{% for endpoint in ['connections','status','rotate','session/list','pool/status'] %}<p class="copy-value">/api/{{ endpoint }}</p><button type="button" class="btn-blue" onclick="copyApi('{{ endpoint }}')">复制 {{ endpoint }}</button>{% endfor %}
</div>
</div>
<button type="submit">保存对外连接设置</button>
</form>
</div>
</div>

<div class="card" data-tab="config">
<h2><span class="tag"><span class="ico">A</span>场景 A：直连（无上游代理）</span>{% if settings.scenario == 'A' %}<span class="cur">当前</span>{% endif %}<span class="arr"></span></h2>
<div class="content">
<div class="note">
<b>说明</b>: mihomo 直接用服务器 IP 出口，不经过任何上游代理。<br>
<b>白名单</b>: 不需要<br>
<b>粘性</b>: {% if sticky.enabled %}场景A为直连，无需粘性会话，直接使用 7890/7891{% else %}未开启粘性{% endif %}
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

<div class="card" data-tab="config">
<h2><span class="tag"><span class="ico">P</span>场景 B/C/D：挂代理（SOCKS5 / HTTP）</span>{% if settings.scenario == 'proxy' %}<span class="cur">当前</span>{% endif %}<span class="arr"></span></h2>
<div class="content">
<div class="note">
<b>格式</b>: 每行一个代理 <code>ip:port</code> 或 <code>ip:port:user:pass</code><br>
<b>白名单</b>: 不需要（用账号密码认证）<br>
<b>粘性</b>: {% if sticky.enabled %}开启 - 轮询分配、不限制并发、故障自动切换（端口不变）、连续失败3次剔除{% else %}未开启粘性{% endif %}
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

<div class="card" data-tab="config">
<h2><span class="tag"><span class="ico">E</span>场景 E：API 提取（用到才提取）</span>{% if settings.scenario == 'E' %}<span class="cur">当前</span>{% endif %}<span class="arr"></span></h2>
<div class="content">
<div class="note warning">
<b>前提</b>: 服务器 IP 必须在上游代理平台加白<br>
<b>机制</b>: {% if sticky.enabled %}粘性开启 - acquire 时懒加载提取1个新代理，1任务1IP，10分钟过期，失败不自动切换{% else %}每 2 分钟检测一次，代理过期才提取，不浪费额度{% endif %}
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

<div class="card" data-tab="config">
<h2><span class="tag"><span class="ico">F</span>场景 F：Clash 订阅链接</span>{% if settings.scenario == 'F' %}<span class="cur">当前</span>{% endif %}<span class="arr"></span></h2>
<div class="content">
<div class="note">
<b>用法</b>: 填入 Clash 订阅链接，自动解析为代理列表<br>
<b>支持</b>: Clash、Clash.Meta、Base64/vmess/vless/trojan/ss/hysteria2 订阅格式<br>
<b>粘性模式</b>: {% if sticky.enabled %}直连模式（绑定第一个可用节点，不过期，故障自动切换）/ 轮询模式（10分钟过期）{% else %}未开启粘性{% endif %}
</div>
<form method="post" action="/apply">
<input type="hidden" name="scenario" value="F">
<label>Clash 订阅 URL</label>
<input type="text" name="clash_url" placeholder="https://example.com/sub?token=xxx" value="{{ settings.saved_scenarios.get('F',{}).get('clash_url','') }}">
<label>模式</label>
<select name="f_mode">
<option value="direct" {% if settings.saved_scenarios.get('F',{}).get('mode')!='poll' %}selected{% endif %}>直连模式（粘性，不过期，故障自动切换）</option>
<option value="poll" {% if settings.saved_scenarios.get('F',{}).get('mode')=='poll' %}selected{% endif %}>轮询模式（粘性，10分钟过期）</option>
</select>
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

<div class="card api-card" data-tab="api">
<h2><span class="tag"><span class="ico">API</span>API 文档</span><span class="arr"></span></h2>
<div class="content">
<div class="note">
<b>认证方式</b>：所有 API 均需携带 API Key，两种方式任选：URL 参数 <code>?key=YOUR_KEY</code> 或请求头 <code>X-API-Key: YOUR_KEY</code>。
未授权返回 <code>401</code>。下面所有示例已自动带上当前 Key，可直接复制执行。<br>
<b>Base URL</b>：<code>http://{{ public_host }}:7892</code>
</div>

<h3><span class="m">GET</span> <span class="path">/api/connections</span> <span class="cur" style="margin-left:0">连接信息</span></h3>
<div class="params">返回对外 SOCKS5 / HTTP 入口的地址、端口与认证状态。可用于自动获取入口链接。</div>
<div class="cmd-row"><pre id="api-connections">curl "http://{{ public_host }}:7892/api/connections?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-connections')">复制</button></div>

<h3><span class="m">GET</span> <span class="path">/api/status</span> <span class="cur" style="margin-left:0">状态</span></h3>
<div class="params">返回代理连通性、出口 IP/地区、当前运行模式（A/Proxy/E/F）与粘性开关状态。</div>
<div class="cmd-row"><pre id="api-status">curl "http://{{ public_host }}:7892/api/status?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-status')">复制</button></div>

<h3><span class="m">GET/POST</span> <span class="path">/api/rotate</span> <span class="cur" style="margin-left:0">轮换/刷新</span></h3>
<div class="params">场景 E：立即重新提取代理；其他场景：重启 mihomo 生效。</div>
<div class="cmd-row"><pre id="api-rotate">curl -X POST "http://{{ public_host }}:7892/api/rotate?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-rotate')">复制</button></div>

<h3><span class="m">POST</span> <span class="path">/api/session/acquire</span> <span class="cur" style="margin-left:0">申请粘性会话</span></h3>
<div class="params">body：<code>{"task_id": "my_task"}</code>。返回独立动态端口（40001-40999）与绑定代理。同一 <code>task_id</code> 重复申请返回同一会话（幂等）。</div>
<div class="cmd-row"><pre id="api-acquire">curl -X POST -H 'Content-Type: application/json' -d '{"task_id": "my_task"}' "http://{{ public_host }}:7892/api/session/acquire?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-acquire')">复制</button></div>

<h3><span class="m">GET</span> <span class="path">/api/session/status?task_id=xxx</span> <span class="cur" style="margin-left:0">查询会话</span></h3>
<div class="params">按 <code>task_id</code> 查询会话当前绑定（端口/代理/过期时间）。</div>
<div class="cmd-row"><pre id="api-sstatus">curl "http://{{ public_host }}:7892/api/session/status?key={{ api_key }}&task_id=my_task"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-sstatus')">复制</button></div>

<h3><span class="m">GET</span> <span class="path">/api/session/list</span> <span class="cur" style="margin-left:0">会话列表</span></h3>
<div class="params">列出全部活跃会话（task_id / 端口 / 绑定代理 / 获取与过期时间）。</div>
<div class="cmd-row"><pre id="api-slist">curl "http://{{ public_host }}:7892/api/session/list?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-slist')">复制</button></div>

<h3><span class="m">GET</span> <span class="path">/api/pool/status</span> <span class="cur" style="margin-left:0">代理池状态</span></h3>
<div class="params">返回代理池总数/可用/占用、活跃会话数与每个节点的健康状态。</div>
<div class="cmd-row"><pre id="api-pool">curl "http://{{ public_host }}:7892/api/pool/status?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-pool')">复制</button></div>

<h3><span class="m">POST</span> <span class="path">/api/session/release</span> <span class="cur" style="margin-left:0">释放会话</span></h3>
<div class="params">body：<code>{"task_id": "my_task"}</code>。释放后端口回收，可被其他任务复用。</div>
<div class="cmd-row"><pre id="api-release">curl -X POST -H 'Content-Type: application/json' -d '{"task_id": "my_task"}' "http://{{ public_host }}:7892/api/session/release?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-release')">复制</button></div>

<h3 style="margin-top:22px"><span class="m">用法</span> 粘性会话完整流程</h3>
<div class="params">申请会话 → 得到端口 → 客户端通过 <code>socks5://入口账号:入口密码@服务器IP:40001</code> 接入 → 用完释放。</div>
<div class="cmd-row"><pre id="api-sticky-flow"># 1. 申请（记下返回的 listener_port，例如 40001）
curl -X POST -H 'Content-Type: application/json' -d '{"task_id": "my_task"}' "http://{{ public_host }}:7892/api/session/acquire?key={{ api_key }}"

# 2. 客户端使用（填入入口账号密码，密码含特殊字符需 URL 编码）
#    socks5://sockstest:socks-pass%401@{{ public_host }}:40001

# 3. 用完释放
curl -X POST -H 'Content-Type: application/json' -d '{"task_id": "my_task"}' "http://{{ public_host }}:7892/api/session/release?key={{ api_key }}"</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-sticky-flow')">复制</button></div>

<h3 style="margin-top:22px"><span class="m">用法</span> 场景 E：1 请求 1 IP</h3>
<div class="params">粘性开启 + 场景 E 时，每次 acquire 懒加载提取 1 个新代理并独占端口，10 分钟过期自动清理，失败不自动切换。适合每个请求需要独立 IP 的场景。</div>
<div class="cmd-row"><pre id="api-e-flow"># 每次请求前申请新会话即可获得独立端口与 IP
curl -X POST -H 'Content-Type: application/json' -d '{"task_id": "req_001"}' "http://{{ public_host }}:7892/api/session/acquire?key={{ api_key }}"

# 通过返回的 listener_port 发请求（每个 task_id 一条独立链路）</pre><button type="button" class="btn-blue btn-small" onclick="copyCmd('api-e-flow')">复制</button></div>
</div>
</div>

{% if current_config %}
<div class="card" data-tab="config">
<h2><span class="tag"><span class="ico">C</span>当前配置文件</span><span class="arr"></span></h2>
<div class="content"><pre>{{ current_config }}</pre></div>
</div>
{% endif %}

<script>
function switchTab(name) {
    document.querySelectorAll('#mainTabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.card[data-tab]').forEach(c => {
        c.style.display = c.dataset.tab === name ? '' : 'none';
    });
    window.scrollTo({top: 0});
}
function copyCmd(id) {
    const el = document.getElementById(id);
    copyText(el.textContent.trim());
}
function copyQSLink(kind) {
    const item = terminalSettings[kind];
    const user = item.username || '';
    const password = item.password || '';
    const auth = user && password ? encodeURIComponent(user) + ':' + encodeURIComponent(password) + '@' : '';
    copyText((kind === 'socks' ? 'socks5' : 'http') + '://' + auth + {{ public_host|tojson }} + ':' + item.port);
}
document.querySelectorAll('.card h2').forEach(h2 => {
    h2.addEventListener('click', () => {
        h2.parentElement.classList.toggle('collapsed');
    });
});
switchTab('overview');
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
function toggleEntryMode() {
    const mode = document.getElementById('entryMode').value;
    const uni = document.getElementById('uniCard');
    document.getElementById('dualCard').style.display = mode === 'dual' ? '' : 'none';
    document.getElementById('dualCard2').style.display = mode === 'dual' ? '' : 'none';
    uni.style.display = mode === 'dual' ? 'none' : '';
    document.getElementById('uniTitle').textContent =
        mode === 'http' ? 'HTTP 入口' : (mode === 'socks' ? 'SOCKS5 入口' : '混合单端口');
    updateUniLink();
}
function updateUniLink() {
    const mode = document.getElementById('entryMode').value;
    const port = document.getElementById('entryPort').value;
    const user = document.getElementById('entryUser').value;
    const entered = document.getElementById('entryPass').value;
    const password = entered || terminalSettings.socks.password;
    const auth = user && password ? encodeURIComponent(user) + ':' + encodeURIComponent(password) + '@' : '';
    const host = {{ public_host|tojson }};
    const el = document.getElementById('uniLink');
    if (mode === 'mixed') el.textContent = 'SOCKS5: socks5://' + auth + host + ':' + port + '    HTTP: http://' + auth + host + ':' + port;
    else if (mode === 'socks') el.textContent = 'socks5://' + auth + host + ':' + port;
    else el.textContent = 'http://' + auth + host + ':' + port;
}
function copyUni(scheme) {
    const form = document.getElementById('terminalForm');
    const user = form.elements['entry_username'].value;
    const entered = form.elements['entry_password'].value;
    const password = entered || terminalSettings.socks.password;
    const port = form.elements['entry_port'].value;
    const auth = user && password ? encodeURIComponent(user) + ':' + encodeURIComponent(password) + '@' : '';
    copyText(scheme + '://' + auth + {{ public_host|tojson }} + ':' + port);
}
if (document.getElementById('entryMode')) updateUniLink();
function copyApi(endpoint) {
    copyText('http://' + {{ public_host|tojson }} + ':7892/api/' + endpoint + '?key=' + encodeURIComponent(terminalSettings.api_key));
}
function runAction(act, btn) {
    if (btn.disabled) return;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = act === 'speed' ? '测速中...' : '测试中...';
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 90000);  // 90s 超时，防止一直转圈
    fetch('/action', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'act=' + act, signal: ctrl.signal})
        .then(r => r.text())
        .then(html => { document.documentElement.innerHTML = html; })
        .catch(e => {
            clearTimeout(timer);
            btn.disabled = false;
            btn.textContent = orig;
            notify('请求超时或无响应：' + e);
        });
}
</script>
<div id="toast" class="toast"></div>
{% endif %}
</div>
</body>
</html>'''


def _ensure_test_proxies():
    """容器启动时自动拉起测试代理（10080/10081），避免重启后 proxy 场景测速/测试失败。
    已监听则跳过；脚本不存在或启动失败仅告警，不影响主流程。"""
    script = '/tmp/socks5_proxy3.py'
    for port in (10080, 10081):
        try:
            s = socket.socket()
            try:
                s.settimeout(1)
                if s.connect_ex(('127.0.0.1', port)) == 0:
                    continue  # 已监听，跳过
            finally:
                s.close()
            if not os.path.exists(script):
                continue  # 生产环境无测试脚本，跳过
            subprocess.Popen(['python3', script, str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            logger.info(f'启动测试代理 127.0.0.1:{port}')
        except Exception as e:
            logger.warning(f'启动测试代理失败 {port}: {e}')


if __name__ == '__main__':
    _ensure_test_proxies()
    app.run(host='0.0.0.0', port=7892)