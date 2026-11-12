# -*- coding: utf-8 -*-
"""Mihomo-Relay-WebUI 回归测试：粘性端口分配 / 探活判据 / 池健康状态。

不连真服务器，全部用内存状态 + monkeypatch 打桩，秒级跑完。

跑法（在 Mihomo-Relay-WebUI2 目录下）：
    python -m pytest test_sticky_regression.py -v

这四条是 2026-11-11 在 45.205.27.41 实测出来、而原代码没覆盖的：
  1. 并发 acquire 全拿 40001（_alloc_port 无锁且不占位）
  2. 上游 403 被当成健康（探活只看 returncode + 有没有数字）
  3. test_enabled=false 时死节点直接下发（无任何校验）
  4. /api/pool/status 的 health 字段恒为 healthy（拿 available 冒充）
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import types

import pytest

APP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')


def _load_app_module():
    """加载 app.py，但把所有 /opt/mihomo-relay/* 常量改到临时目录。

    app.py 在 import 期就会调 `_sync_sticky_from_settings()` → `load_settings()`，
    读不到 /opt/mihomo-relay/terminal_settings.json 时会写盘并抛 FileNotFoundError。
    所以必须在**编译前**把路径常量替换掉，不能等 import 完再 monkeypatch。
    """
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix='mihomo-test-')

    # 打桩 docker，避免 import 失败
    if 'docker' not in sys.modules:
        stub = types.ModuleType('docker')
        stub.from_env = lambda: None
        stub.errors = types.SimpleNamespace(NotFound=type('NotFound', (Exception,), {}))
        sys.modules['docker'] = stub

    src = open(APP_PATH, encoding='utf-8').read()
    for const, fname in (
        ('CONFIG_PATH', 'mihomo_config.yaml'),
        ('PROVIDERS_PATH', 'mihomo_providers.yaml'),
        ('REFRESH_SCRIPT', 'cliproxy_refresh.sh'),
        ('SETTINGS_PATH', 'terminal_settings.json'),
    ):
        m = re.search(rf"^{const} = '[^']*'$", src, re.M)
        assert m, f'没找到 {const} 常量定义'
        new_path = os.path.join(tmpdir, fname).replace('\\', '\\\\')
        src = src[:m.start()] + f"{const} = '{new_path}'" + src[m.end():]

    # 停掉后台维护线程：它每 30 秒调一次 _sync_sticky_from_settings()，
    # 会按空配置把 STICKY_STATE['enabled'] 刷回 False，测试中途随机失败。
    startup = 'threading.Thread(target=_sticky_maintenance_loop, daemon=True).start()'
    assert startup in src, '没找到维护线程启动语句'
    src = src.replace(startup, 'pass  # 测试环境不启动维护线程')

    mod = types.ModuleType('mihomo_web_under_test')
    mod.__file__ = APP_PATH
    sys.modules['mihomo_web_under_test'] = mod
    exec(compile(src, APP_PATH, 'exec'), mod.__dict__)
    return mod


@pytest.fixture(scope='module')
def app():
    return _load_app_module()


@pytest.fixture(autouse=True)
def _isolate_state(app, monkeypatch):
    """每个用例：洗干净全局状态 + 还原所有被打桩的函数。"""
    real_run = app.subprocess.run
    real_load = app.load_settings
    real_extract = app._extract_from_api
    real_parse = app._parse_proxy_line
    real_direct = getattr(app, '_probe_node_direct', None)
    real_reload = app._reload_sticky
    real_authorized = app.api_authorized

    def reset():
        with app.STICKY_LOCK:
            # setdefault：老版本没有 'pending' 键，别让 fixture 自己先炸掉
            for k in ('sessions', 'ports', 'pending', 'pool'):
                app.STICKY_STATE.setdefault(k, {}).clear()
            app.STICKY_STATE['f_pool'] = []
            app.STICKY_STATE['e_test_nodes'] = []
            app.STICKY_STATE['rr'] = 0
        app.STICKY_STATE['enabled'] = True
        app.STICKY_STATE['test_enabled'] = True
        app.STICKY_STATE['test_url'] = 'http://ip-api.com/json'

    reset()
    yield
    reset()
    app.subprocess.run = real_run
    app.load_settings = real_load
    app._extract_from_api = real_extract
    app._parse_proxy_line = real_parse
    if real_direct is not None:
        app._probe_node_direct = real_direct
    app._reload_sticky = real_reload
    app.api_authorized = real_authorized


# ══ 1. 并发端口分配：40001 撞车的根因 ═══════════════════════════════════

def _alloc_or_skip(app, reserve):
    """旧版 _alloc_port 没有 reserve_for 参数，直接调会 TypeError。

    这种情况本身就是 bug（不占位），用 pytest.fail 明确指出来，
    而不是吐一个看不懂的 TypeError。
    """
    try:
        return app._alloc_port(reserve_for=reserve)
    except TypeError:
        pytest.fail('_alloc_port 不支持 reserve_for —— 端口分配没有占位，并发必然撞车')

def test_alloc_port_is_unique_under_concurrency(app):
    """16 线程同时抢端口，必须拿到 16 个互不相同的端口。

    旧实现只读 ports 不写，实测 4 并发全部返回 40001。
    """
    n = 16
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()                       # 同时开跑，最大化竞态窗口
        port = _alloc_or_skip(app, f'pending:t{i}')
        with lock:
            results.append(port)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n
    assert all(p is not None for p in results), f'有线程没分到端口：{results}'
    assert len(set(results)) == n, f'端口重复：{sorted(results)}'


def test_alloc_port_reserves_immediately(app):
    """分配即占位：连续两次分配不能拿到同一个端口。"""
    p1 = _alloc_or_skip(app, 'pending:a')
    p2 = _alloc_or_skip(app, 'pending:b')
    assert p1 != p2
    with app.STICKY_LOCK:
        assert app.STICKY_STATE['ports'][p1] == 'pending:a'
        assert app.STICKY_STATE['pending'][p1] > 0


def test_store_session_promotes_pending(app):
    """_store_session 转正后 pending 消失，ports 指向真 task_id。"""
    port = _alloc_or_skip(app, 'pending:reg-1')
    app._store_session({'task_id': 'reg-1', 'port': port, 'scenario': 'E',
                        'ref': 'e_x', 'created': time.time(), 'expires': None})
    with app.STICKY_LOCK:
        assert app.STICKY_STATE['ports'][port] == 'reg-1'
        assert port not in app.STICKY_STATE['pending']


def test_stale_pending_is_reclaimed(app):
    """僵死占位要被维护线程回收，否则端口永久泄漏。"""
    port = _alloc_or_skip(app, 'pending:ghost')
    ttl = getattr(app, 'PENDING_TTL', 180)
    with app.STICKY_LOCK:
        app.STICKY_STATE['pending'][port] = time.time() - (ttl + 10)
    app._sticky_cleanup()
    with app.STICKY_LOCK:
        assert port not in app.STICKY_STATE['ports']
        assert port not in app.STICKY_STATE['pending']


def test_fresh_pending_survives_cleanup(app):
    """刚开始的占位不能被误清（还有 acquire 在提取中）。"""
    port = _alloc_or_skip(app, 'pending:busy')
    app._sticky_cleanup()
    with app.STICKY_LOCK:
        assert app.STICKY_STATE['ports'][port] == 'pending:busy'


# ══ 2. 探活判据：403 不能被当成健康 ════════════════════════════════════

class _FakeProc:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out
        self.stderr = ''


def _stub_curl(app, rc, code):
    app.subprocess.run = lambda cmd, **kwargs: _FakeProc(rc, code)


def test_probe_rejects_403(app):
    """上游 403（白名单没加/账密错）必须判不可用。"""
    _stub_curl(app, 0, '403')
    assert app._test_through_port(40001) is False


def test_probe_rejects_407(app):
    _stub_curl(app, 0, '407')
    assert app._test_through_port(40001) is False


def test_probe_accepts_200(app):
    _stub_curl(app, 0, '200')
    assert app._test_through_port(40001) is True


def test_probe_accepts_302(app):
    _stub_curl(app, 0, '302')
    assert app._test_through_port(40001) is True


def test_probe_rejects_curl_error(app):
    """curl 自己失败（连不上/超时）不能因为 stdout 有数字就算通。"""
    _stub_curl(app, 52, '000')
    assert app._test_through_port(40001) is False


# ══ 3. 直探：test_enabled=false 时的最后一道拦截 ═══════════════════════

def test_probe_node_direct_uses_upstream_not_listener(app):
    """直探必须打上游地址，不能误用 listener 端口。"""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen['cmd'] = cmd
        return _FakeProc(0, '200')

    app.subprocess.run = fake_run
    node = {'server': '1.2.3.4', 'port': 8080, 'type': 'http'}
    assert app._probe_node_direct(node) is True
    joined = ' '.join(seen['cmd'])
    assert '1.2.3.4:8080' in joined
    assert ':40001' not in joined


def test_probe_node_direct_blocks_dead_node(app):
    """死节点直探必须返回 False，让 _acquire_e 换下一条。"""
    _stub_curl(app, 52, '000')
    node = {'server': '9.9.9.9', 'port': 3128, 'type': 'http'}
    assert app._probe_node_direct(node) is False


def test_probe_node_direct_socks5_with_auth(app):
    """带认证 socks5 要拼成 socks5h://user:pass@host:port，密码特殊字符要转义。"""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen['cmd'] = cmd
        return _FakeProc(0, '200')

    app.subprocess.run = fake_run
    node = {'server': '2.2.2.2', 'port': 1080, 'type': 'socks5',
            'username': 'u', 'password': 'p@ss'}
    assert app._probe_node_direct(node) is True
    joined = ' '.join(seen['cmd'])
    assert 'socks5h://' in joined
    assert 'p%40ss' in joined, '密码里的 @ 必须转义'


# ══ 4. 池健康状态不能撒谎 ══════════════════════════════════════════════

def _pool_status(app, entry):
    with app.STICKY_LOCK:
        app.STICKY_STATE['pool'] = {'k1': entry}
    app.api_authorized = lambda: True
    with app.app.test_request_context('/api/pool/status?key=x'):
        return app.api_pool_status().get_json()


def test_pool_health_unknown_before_probe(app):
    """没探过的节点显示 unknown，不能冒充 healthy。"""
    data = _pool_status(app, {
        'key': 'k1', 'ref': 'n1', 'proxy': '1.1.1.1:80',
        'in_use': 0, 'failures': 0, 'available': True,
        'probed': False, 'last_ok': None, 'last_probe': 0})
    assert data['pool']['proxies'][0]['health'] == 'unknown'


def test_pool_health_unhealthy_after_failed_probe(app):
    data = _pool_status(app, {
        'key': 'k1', 'ref': 'n1', 'proxy': '1.1.1.1:80',
        'in_use': 0, 'failures': 0, 'available': True,
        'probed': True, 'last_ok': False, 'last_probe': time.time()})
    assert data['pool']['proxies'][0]['health'] == 'unhealthy'


def test_pool_health_healthy_after_ok_probe(app):
    data = _pool_status(app, {
        'key': 'k1', 'ref': 'n1', 'proxy': '1.1.1.1:80',
        'in_use': 0, 'failures': 0, 'available': True,
        'probed': True, 'last_ok': True, 'last_probe': time.time()})
    assert data['pool']['proxies'][0]['health'] == 'healthy'


def test_pool_status_no_longer_claims_healthy_without_probe(app):
    """回归：旧代码 available=True 就回 'healthy'，实测 8 个节点全 403 也报 healthy。"""
    data = _pool_status(app, {
        'key': 'k1', 'ref': 'n1', 'proxy': '1.1.1.1:80',
        'in_use': 0, 'failures': 0, 'available': True})
    assert data['pool']['proxies'][0]['health'] != 'healthy'


# ══ 5. E 场景端到端：并发 acquire 必须拿到不同端口 ═════════════════════

def _stub_e_plumbing(app, extract):
    app._extract_from_api = extract

    def fake_parse(line):
        host, port = line.rsplit(':', 1)
        return {'server': host, 'port': int(port), 'type': 'http'}

    app._parse_proxy_line = fake_parse
    app._probe_node_direct = lambda node, timeout=8: True
    app._reload_sticky = lambda settings=None: (True, None)
    app._test_through_port = lambda port, timeout=8: True
    app.load_settings = lambda: {
        'scenario': 'E',
        'saved_scenarios': {'E': {'api_url': 'http://fake/api'}},
        'socks': {'enabled': True, 'port': 7890},
        'http': {'enabled': True, 'port': 7891},
        'sticky': {'enabled': True, 'test_enabled': True},
    }
    app.STICKY_STATE['enabled'] = True
    app.STICKY_STATE['test_enabled'] = True


def test_concurrent_e_acquire_gets_distinct_ports(app):
    """E 场景 4 个并发 acquire，端口必须互不相同。

    原始问题：4 并发全拿到 40001，四条上游挤一个 listener，出口 IP 全一样。
    """
    counter = {'n': 0}
    lock = threading.Lock()

    def fake_extract(api_url, num=1):
        # 真实 _extract_from_api 是一次 curl，耗时 2-5 秒。这段延迟正是
        # 「_alloc_port 拿到端口」到「_store_session 占位」之间的竞态窗口，
        # 必须还原出来，否则测试跑不出并发撞端口。
        time.sleep(0.35)
        with lock:
            counter['n'] += 1
            i = counter['n']
        return [f'10.0.0.{i}:{9000 + i}']

    _stub_e_plumbing(app, fake_extract)
    app._probe_node_direct = lambda node, timeout=8: True

    results = []
    rlock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker(i):
        barrier.wait()
        session, err = app.acquire_session(f'e-conc-{i}')
        with rlock:
            results.append((session, err))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 4
    failed = [(s, e) for s, e in results if not s]
    assert not failed, f'有 acquire 失败：{failed}'
    ports = [s['port'] for s, _ in results]
    assert len(set(ports)) == 4, f'端口重复：{sorted(ports)}'


def test_e_acquire_retries_dead_node(app):
    """第一条提取节点是死的，必须换下一条，不能直接下发。"""
    calls = {'n': 0}

    def fake_extract(api_url, num=1):
        calls['n'] += 1
        return [f'9.9.9.{calls["n"]}:1111']

    _stub_e_plumbing(app, fake_extract)

    probes = []

    def fake_probe(node, timeout=8):
        probes.append(node['server'])
        return node['server'] != '9.9.9.1'      # 第一个死，第二个活

    app._probe_node_direct = fake_probe
    app._test_through_port = lambda port, timeout=8: True

    session, err = app.acquire_session('e-retry')
    assert session is not None, err
    assert probes == ['9.9.9.1', '9.9.9.2'], probes
    assert session['proxy'] == '9.9.9.2:1111'


def test_e_acquire_cleans_pending_on_total_failure(app):
    """三次提取都失败时，占位端口必须归还，不能泄漏。"""
    _stub_e_plumbing(app, lambda api_url, num=1: [])
    app._test_through_port = lambda port, timeout=8: True

    session, err = app.acquire_session('e-fail')
    assert session is None
    assert '重试' in err
    with app.STICKY_LOCK:
        assert not app.STICKY_STATE['ports'], f'端口泄漏：{app.STICKY_STATE["ports"]}'
        assert not app.STICKY_STATE['pending'], f'占位泄漏：{app.STICKY_STATE["pending"]}'
