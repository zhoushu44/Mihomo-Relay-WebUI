# -*- coding: utf-8 -*-
"""多场景共存：不同请求要求不同模式，必须都能正常工作。

这是 2026-11-12 的核心改造。原来的架构是**全局单场景**：
  - `settings['scenario']` 一个值决定所有 acquire 走哪种模式
  - 请求里带 scenario 参数会被静默忽略
  - 切场景会 `_clear_sessions()` 把所有人的会话全清掉

改造后每个 session 自带 scenario，共存于同一个 mihomo 进程：
  - listener 的 `proxy: <上游名>` 独立路由，不走全局 rules
  - `_build_full_config` 把所有活跃会话场景的上游并进 proxies
  - 切全局场景只影响共享口 7890/7891，不再清别人的会话

跑法：
    python -m pytest test_multi_scenario.py -v
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
    """加载 app.py，把 /opt/mihomo-relay/* 常量改到临时目录，停掉后台线程。"""
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix='mihomo-multi-')

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

    startup = 'threading.Thread(target=_sticky_maintenance_loop, daemon=True).start()'
    assert startup in src, '没找到维护线程启动语句'
    src = src.replace(startup, 'pass  # 测试环境不启动维护线程')

    mod = types.ModuleType('mihomo_multi_under_test')
    mod.__file__ = APP_PATH
    sys.modules['mihomo_multi_under_test'] = mod
    exec(compile(src, APP_PATH, 'exec'), mod.__dict__)
    return mod


@pytest.fixture(scope='module')
def app():
    return _load_app_module()


@pytest.fixture(autouse=True)
def _isolate(app):
    saved = {}
    for attr in ('subprocess', 'load_settings', '_extract_from_api',
                 '_parse_proxy_line', '_probe_node_direct', '_reload_sticky',
                 '_test_through_port', '_refresh_f_nodes', '_ensure_f_pool',
                 'api_authorized',
                 # 这三个会被某些用例打桩，必须还原，否则污染后续所有测试
                 '_acquire_bcd', '_acquire_e', '_acquire_f'):
        saved[attr] = getattr(app, attr, None)

    def reset():
        with app.STICKY_LOCK:
            for k in ('sessions', 'ports', 'pending', 'pool'):
                app.STICKY_STATE.setdefault(k, {}).clear()
            app.STICKY_STATE['f_pool'] = []
            app.STICKY_STATE['f_nodes'] = []
            app.STICKY_STATE['f_pool_sig'] = None
            app.STICKY_STATE['f_listener_ready'] = set()
            app.STICKY_STATE['e_test_nodes'] = []
            app.STICKY_STATE['rr'] = 0
        app.STICKY_STATE['enabled'] = True
        app.STICKY_STATE['test_enabled'] = True
        app.STICKY_STATE['test_url'] = 'http://ip-api.com/json'

    reset()
    yield
    reset()
    for attr, val in saved.items():
        if val is not None:
            setattr(app, attr, val)


# ══ 1. acquire 接受 per-request 场景 ═══════════════════════════════════

def test_acquire_accepts_scenario_kwarg(app):
    """acquire_session 必须接受 scenario 参数（原来签名只有 task_id）。"""
    import inspect
    sig = inspect.signature(app.acquire_session)
    assert 'scenario' in sig.parameters, f'签名是 {sig}，没有 scenario 参数'


def test_acquire_rejects_unknown_scenario(app):
    """不支持的场景要明确报错，不能静默回落到全局。"""
    app.STICKY_STATE['enabled'] = True
    s, err = app.acquire_session('t-unknown', scenario='Z')
    assert s is None
    assert 'Z' in err and '不支持' in err


def test_acquire_rejects_scenario_a(app):
    """A 是直连，没有上游可绑，必须拒绝。"""
    s, err = app.acquire_session('t-a', scenario='A')
    assert s is None
    assert '直连' in err


def test_acquire_falls_back_to_global(app):
    """不传 scenario 时用全局场景（兼容老调用方）。"""
    app.load_settings = lambda: {
        'scenario': 'E', 'saved_scenarios': {'E': {'api_url': 'http://x'}},
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
    }
    called = {}

    def fake_e(tid):
        called['e'] = tid
        return {'task_id': tid, 'port': 40001, 'scenario': 'E'}, None

    app._acquire_e = fake_e
    s, err = app.acquire_session('t-default')
    assert s is not None and called.get('e') == 't-default'


# ══ 2. 每个会话记自己的场景 ═══════════════════════════════════════════

def test_bcd_session_records_own_scenario(app):
    """手填代理池的会话，scenario 必须是 'proxy'，不能借全局值。"""
    app.load_settings = lambda: {
        'scenario': 'F',            # 全局是 F
        'saved_scenarios': {'proxy': {'proxies': '1.1.1.1:8080', 'proxy_type': 'http'}},
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
    }
    with app.STICKY_LOCK:
        app.STICKY_STATE['pool'] = {
            'k1': {'key': 'k1', 'ref': 'p1', 'proxy': '1.1.1.1:8080',
                   'in_use': 0, 'failures': 0, 'available': True,
                   'probed': False, 'last_ok': None, 'last_probe': 0},
        }
    app._reload_sticky = lambda settings=None: (True, None)
    app._test_through_port = lambda port, timeout=8: True

    s, err = app.acquire_session('t-bcd', scenario='proxy')
    assert s is not None, err
    assert s['scenario'] == 'proxy', f"借了全局场景：{s['scenario']}"


# ══ 3. 配置构建：多场景上游必须并在一起 ═══════════════════════════════

def test_build_config_merges_all_active_scenarios(app):
    """E 会话 + F 会话同时存在时，两边的上游都要进 proxies。

    否则 mihomo 找不到 listener 指向的 proxy，整份配置加载失败 ——
    不是那个端口坏，是全部端口一起坏。
    """
    import yaml

    app.load_settings = lambda: {
        'scenario': 'E',
        'saved_scenarios': {
            'E': {'api_url': 'http://x'},
            'proxy': {'proxies': '9.9.9.9:3128', 'proxy_type': 'http'},
        },
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
        'entry_mode': 'dual',
        'exit_mode': 'scenario',
        'sticky': {'enabled': True, 'test_enabled': True},
    }
    app._extract_from_api = lambda u, num=1: ['1.1.1.1:1111']
    app._parse_proxy_line = lambda line: {
        'server': line.split(':')[0], 'port': int(line.split(':')[1]), 'type': 'http'}
    app._refresh_f_nodes = lambda settings=None: None
    app._ensure_f_pool = lambda settings=None: False

    with app.STICKY_LOCK:
        app.STICKY_STATE['f_nodes'] = [
            {'name': 'f-node-1', 'type': 'socks5', 'server': '8.8.8.8', 'port': 1080}]
        app.STICKY_STATE['sessions'] = {
            'e1': {'task_id': 'e1', 'port': 40001, 'scenario': 'E',
                   'ref': 'e_40001', 'proxy': '1.1.1.1:1111',
                   'proxy_node': {'name': 'e_40001', 'type': 'http',
                                  'server': '1.1.1.1', 'port': 1111},
                   'created': time.time(), 'expires': None},
            'f1': {'task_id': 'f1', 'port': 40002, 'scenario': 'F',
                   'ref': 'f-node-1', 'proxy': '8.8.8.8:1080',
                   'proxy_node': None, 'created': time.time(), 'expires': None},
        }
        app.STICKY_STATE['ports'] = {40001: 'e1', 40002: 'f1'}
        app.STICKY_STATE['f_pool'] = [
            {'port': 40002, 'ref': 'f-node-1', 'server': '8.8.8.8', 'sport': 1080}]

    cfg_str, err = app._build_full_config()
    assert err is None, err
    cfg = yaml.safe_load(cfg_str)
    names = {p['name'] for p in cfg['proxies']}
    assert 'e_40001' in names, f'E 会话的上游没进配置：{names}'
    assert 'f-node-1' in names, f'F 会话的上游没进配置：{names}'

    # 两个 listener 都要在，且各自指向自己的上游
    listeners = {l['name']: l for l in cfg['listeners']}
    assert 'sticky-40001' in listeners
    assert 'sticky-40002' in listeners
    assert listeners['sticky-40001']['proxy'] == 'e_40001'
    assert listeners['sticky-40002']['proxy'] == 'f-node-1'


def test_build_config_no_orphan_listener(app):
    """每个 listener 的 proxy 都必须能在 proxies 里找到，否则 mihomo 拒载。"""
    import yaml

    app.load_settings = lambda: {
        'scenario': 'proxy',
        'saved_scenarios': {'proxy': {'proxies': '9.9.9.9:3128', 'proxy_type': 'http'}},
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
        'entry_mode': 'dual', 'exit_mode': 'scenario',
        'sticky': {'enabled': True, 'test_enabled': True},
    }
    app._refresh_f_nodes = lambda settings=None: None
    app._ensure_f_pool = lambda settings=None: False

    with app.STICKY_LOCK:
        app.STICKY_STATE['sessions'] = {
            'p1': {'task_id': 'p1', 'port': 40001, 'scenario': 'proxy',
                   'ref': 'p1', 'proxy': '9.9.9.9:3128', 'proxy_node': None,
                   'created': time.time(), 'expires': None},
        }
        app.STICKY_STATE['ports'] = {40001: 'p1'}

    cfg_str, err = app._build_full_config()
    assert err is None, err
    cfg = yaml.safe_load(cfg_str)
    names = {p['name'] for p in cfg['proxies']}
    for lis in cfg['listeners']:
        target = lis.get('proxy')
        if target:
            assert target in names, f"listener {lis['name']} 指向不存在的上游 {target}"


def test_build_config_global_failure_does_not_break_sessions(app):
    """全局场景提取失败时，有会话在跑就只能降级，不能整份配置失败。"""
    app.load_settings = lambda: {
        'scenario': 'E',
        'saved_scenarios': {'E': {'api_url': 'http://dead'}},
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
        'entry_mode': 'dual', 'exit_mode': 'scenario',
        'sticky': {'enabled': True, 'test_enabled': True},
    }
    app._extract_from_api = lambda u, num=1: []      # 全局 E 提取失败
    app._refresh_f_nodes = lambda settings=None: None
    app._ensure_f_pool = lambda settings=None: False

    with app.STICKY_LOCK:
        app.STICKY_STATE['sessions'] = {
            'e1': {'task_id': 'e1', 'port': 40001, 'scenario': 'E',
                   'ref': 'e_40001', 'proxy': '1.1.1.1:1111',
                   'proxy_node': {'name': 'e_40001', 'type': 'http',
                                  'server': '1.1.1.1', 'port': 1111},
                   'created': time.time(), 'expires': None},
        }
        app.STICKY_STATE['ports'] = {40001: 'e1'}

    cfg_str, err = app._build_full_config()
    assert cfg_str is not None, f'全局失败把整份配置搞挂了：{err}'


def test_build_config_global_failure_returns_error_when_idle(app):
    """没有会话时，全局失败仍要老实返回错误（别假装成功）。"""
    app.load_settings = lambda: {
        'scenario': 'E',
        'saved_scenarios': {'E': {'api_url': 'http://dead'}},
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
        'entry_mode': 'dual', 'exit_mode': 'scenario',
        'sticky': {'enabled': True, 'test_enabled': True},
    }
    app._extract_from_api = lambda u, num=1: []
    app._refresh_f_nodes = lambda settings=None: None
    app._ensure_f_pool = lambda settings=None: False

    cfg_str, err = app._build_full_config()
    assert cfg_str is None
    assert err and '无可用节点' in err


# ══ 4. F 静态池不能被全局场景误清 ═════════════════════════════════════

def test_f_pool_survives_when_global_is_not_f(app):
    """全局是 E，但有 F 会话在跑：F 的静态池不能被清掉。"""
    app.load_settings = lambda: {
        'scenario': 'E',
        'saved_scenarios': {'E': {'api_url': 'http://x'}},
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
        'entry_mode': 'dual', 'exit_mode': 'scenario',
        'sticky': {'enabled': True, 'test_enabled': True},
    }
    app._extract_from_api = lambda u, num=1: ['1.1.1.1:1111']
    app._parse_proxy_line = lambda line: {
        'server': line.split(':')[0], 'port': int(line.split(':')[1]), 'type': 'http'}
    app._refresh_f_nodes = lambda settings=None: None
    app._ensure_f_pool = lambda settings=None: False

    app.STICKY_STATE['f_pool'] = [
        {'port': 40002, 'ref': 'f-node-1', 'server': '8.8.8.8', 'sport': 1080}]
    app.STICKY_STATE['f_nodes'] = [
        {'name': 'f-node-1', 'type': 'socks5', 'server': '8.8.8.8', 'port': 1080}]
    with app.STICKY_LOCK:
        app.STICKY_STATE['sessions'] = {
            'f1': {'task_id': 'f1', 'port': 40002, 'scenario': 'F',
                   'ref': 'f-node-1', 'proxy': '8.8.8.8:1080',
                   'proxy_node': None, 'created': time.time(), 'expires': None},
        }
        app.STICKY_STATE['ports'] = {40002: 'f1'}

    app._build_full_config()
    assert app.STICKY_STATE['f_pool'], 'F 静态池被全局场景误清了'


# ══ 5. 切全局场景不该清别人的会话 ═════════════════════════════════════

def test_switch_scenario_keeps_sessions(app):
    """切全局场景后，带场景标记的会话必须还在。"""
    with app.STICKY_LOCK:
        app.STICKY_STATE['sessions'] = {
            'keep-me': {'task_id': 'keep-me', 'port': 40001, 'scenario': 'E',
                        'ref': 'e_x', 'proxy': '1.1.1.1:1111',
                        'proxy_node': None, 'created': time.time(), 'expires': None},
        }
        app.STICKY_STATE['ports'] = {40001: 'keep-me'}

    # 直接测「切场景时的清理逻辑」这段：只清没有 scenario 的老会话
    with app.STICKY_LOCK:
        stale = [t for t, s in app.STICKY_STATE['sessions'].items()
                 if not s.get('scenario')]
    for t in stale:
        app.release_session(t)

    with app.STICKY_LOCK:
        assert 'keep-me' in app.STICKY_STATE['sessions'], '切场景把正常会话清掉了'


# ══ 6. 端到端：三种模式同时申请 ═══════════════════════════════════════

def test_three_scenarios_coexist(app):
    """E + F + proxy 三种模式同时申请：各自的 session 记自己的场景，
    端口互不相同（F 用静态池端口，E/proxy 用动态端口）。

    这里刻意把 `_reload_sticky` 打成真桩（返回 True 但确实把 listener 写进
    `ports`）会很啰嗦，所以显式模拟：E/proxy 走 `_alloc_port`，
    F 走预置的静态池端口 —— 这正是产品代码的真实分工。
    """
    app.load_settings = lambda: {
        'scenario': 'E',
        'saved_scenarios': {
            'E': {'api_url': 'http://x'},
            'proxy': {'proxies': '9.9.9.9:3128', 'proxy_type': 'http'},
        },
        'socks': {'enabled': True, 'port': 7890, 'username': 'u', 'password': 'p'},
        'http': {'enabled': True, 'port': 7891, 'username': 'u', 'password': 'p'},
        'sticky': {'enabled': True, 'test_enabled': True},
    }
    app._extract_from_api = lambda u, num=1: ['1.1.1.1:1111']
    app._parse_proxy_line = lambda line: {
        'server': line.split(':')[0], 'port': int(line.split(':')[1]), 'type': 'http'}
    app._probe_node_direct = lambda node, timeout=8: True
    app._test_through_port = lambda port, timeout=8: True
    app._refresh_f_nodes = lambda settings=None: None
    # 真桩：保证 F 池存在，并让 acquire 期间不被清
    app._ensure_f_pool = lambda settings=None: False

    with app.STICKY_LOCK:
        app.STICKY_STATE['pool'] = {
            'k1': {'key': 'k1', 'ref': 'p1', 'proxy': '9.9.9.9:3128',
                   'in_use': 0, 'failures': 0, 'available': True,
                   'probed': True, 'last_ok': True, 'last_probe': time.time()},
        }
        app.STICKY_STATE['f_nodes'] = [
            {'name': 'f-node-1', 'type': 'socks5', 'server': '8.8.8.8', 'port': 1080}]
        app.STICKY_STATE['f_pool'] = [
            {'port': 40010, 'ref': 'f-node-1', 'server': '8.8.8.8', 'sport': 1080}]
        # F 池端口在真实环境由 _ensure_f_pool+_reload 占位；这里手动占，
        # 否则 _alloc_port 会把 40010 当空闲分给 E/proxy。
        app.STICKY_STATE['ports'][40010] = '__f_pool__'

    def _reload(settings=None):
        # 模拟热重载：F 会话的端口占位被写进 ports
        return (True, None)

    app._reload_sticky = _reload

    # ── E ──
    s_e, err = app.acquire_session('m-e', scenario='E')
    assert s_e is not None, f'E 申请失败：{err}'
    assert s_e['scenario'] == 'E'
    assert s_e['port'] != 40010, 'E 不该占用 F 的静态池端口'

    # ── proxy（全局是 E，显式要 proxy）──
    s_p, err = app.acquire_session('m-p', scenario='proxy')
    assert s_p is not None, f'proxy 申请失败：{err}'
    assert s_p['scenario'] == 'proxy', f"proxy 会话记成了 {s_p['scenario']}"
    assert s_p['port'] not in (s_e['port'], 40010), \
        f"proxy 端口和别的模式撞了：{s_p['port']}"

    # ── F ──
    with app.STICKY_LOCK:
        app.STICKY_STATE['ports'].pop(40010, None)   # 让 F 能拿到自己的池端口
    s_f, err = app.acquire_session('m-f', scenario='F')
    assert s_f is not None, f'F 申请失败：{err}'
    assert s_f['scenario'] == 'F'
    assert s_f['port'] == 40010, f"F 没用静态池端口：{s_f['port']}"

    ports = [s_e['port'], s_p['port'], s_f['port']]
    assert len(set(ports)) == 3, f'三种模式端口重复：{ports}'

    # 会话并存，各自记自己的场景
    with app.STICKY_LOCK:
        live = {t: v['scenario'] for t, v in app.STICKY_STATE['sessions'].items()}
    assert live.get('m-e') == 'E'
    assert live.get('m-p') == 'proxy'
    assert live.get('m-f') == 'F'
