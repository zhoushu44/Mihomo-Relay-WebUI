# -*- coding: utf-8 -*-
"""场景参数不能被空值覆盖。

复现过的真实事故（2026-11-12，两次）：
    POST /api/v1/config {"scenario":"E"}   # 只想切过去，没带 api_url
    → E 的 api_url 被写成 ""，整套 E 模式失效
    → 之后 acquire 全部返回「请先配置 API URL」

根因：
  - `apply()` 无条件 `saved[scenario] = params`，表单没填就是空串
  - `_exec_apply()` 非 switch 分支 `params = req_params` 同样含空值

跑法：
    python -m pytest test_config_preserve.py -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import types

import pytest

APP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')


def _load_app_module():
    """加载 app.py，把 /opt/mihomo-relay/* 常量改到临时目录，停掉后台线程。"""
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix='mihomo-cfg-')

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
    if startup in src:
        src = src.replace(startup, 'pass  # 测试环境不启动维护线程')

    mod = types.ModuleType('mihomo_cfg_under_test')
    mod.__file__ = APP_PATH
    sys.modules['mihomo_cfg_under_test'] = mod
    exec(compile(src, APP_PATH, 'exec'), mod.__dict__)
    return mod


@pytest.fixture(scope='module')
def app():
    return _load_app_module()


@pytest.fixture(autouse=True)
def _isolate(app):
    """每个用例写入一份已知配置，用完还原。"""
    original = app.load_settings()

    def reset(**scenarios):
        s = app.load_settings()
        s['saved_scenarios'] = dict(scenarios) if scenarios else {
            'E': {'api_url': 'http://api.example/extract?num=1', 'api_num': '1'},
            'F': {'clash_url': 'http://sub.example/clash', 'mode': 'poll'},
            'proxy': {'proxy_type': 'http', 'proxies': '1.1.1.1:8080',
                      'username': '', 'password': '', 'rotate': 'yes'},
            'A': {'configured': True},
        }
        s['scenario'] = 'E'
        # 粘性开关写进 settings —— load_settings() 会从磁盘重读，
        # 只改 STICKY_STATE 会被覆盖。
        s.setdefault('sticky', {})['enabled'] = True
        app.save_settings(s)

    # 打桩：apply() 后面会调 deploy_mihomo / gen_*_config，不能在测试里真跑
    app.deploy_mihomo = lambda cfg, **kw: (True, None)
    app.gen_direct_config = lambda: {'rules': ['MATCH,DIRECT']}
    app.gen_api_config = lambda url, num: {'rules': [], 'proxies': []}
    app.gen_proxy_config = lambda *a, **kw: ({'rules': []}, None)

    reset()
    yield
    app.save_settings(original)


def _saved(app):
    return app.load_settings().get('saved_scenarios', {})


def _set_sticky(app, on=True):
    """改 sticky.enabled 并同步到内存 —— load_settings 会从磁盘重读。"""
    s = app.load_settings()
    s.setdefault('sticky', {})['enabled'] = on
    app.save_settings(s)
    app._sync_sticky_from_settings()


# ══ 1. 合并器本身 ══════════════════════════════════════════════════════

def test_merge_keeps_existing_when_incoming_empty(app):
    """空字符串不能覆盖已有值。"""
    merged, status = app._merge_scenario_params(
        {'api_url': 'http://old', 'api_num': '1'},
        {'api_url': '', 'api_num': '1'},
        'E')
    assert merged['api_url'] == 'http://old'


def test_merge_applies_non_empty(app):
    """有值的字段要能正常更新。"""
    merged, status = app._merge_scenario_params(
        {'api_url': 'http://old'},
        {'api_url': 'http://new'},
        'E')
    assert merged['api_url'] == 'http://new'


def test_merge_rejects_when_key_field_missing(app):
    """关键字段为空且无历史值 → 明确拒绝，而不是写个废配置。"""
    merged, status = app._merge_scenario_params({}, {'api_url': ''}, 'E')
    assert merged is None
    assert status == 'missing_key_field'


def test_merge_allows_other_fields_empty(app):
    """非关键字段（username/password）空着没关系。"""
    merged, status = app._merge_scenario_params(
        {'proxies': '1.1.1.1:8080'},
        {'proxies': '1.1.1.1:8080', 'username': '', 'password': ''},
        'proxy')
    assert merged is not None
    assert merged['proxies'] == '1.1.1.1:8080'


# ══ 2. _exec_apply：这就是线上两次事故的入口 ═════════════════════════

def test_exec_apply_empty_payload_preserves_e(app):
    """POST {"scenario":"E"} 不能再清空 api_url —— 这是真实事故。"""
    app._sync_sticky_from_settings()
    app._exec_apply({'scenario': 'E'})
    assert _saved(app)['E']['api_url'] == 'http://api.example/extract?num=1'


def test_exec_apply_empty_payload_preserves_f(app):
    app._sync_sticky_from_settings()
    app._exec_apply({'scenario': 'F'})
    assert _saved(app)['F']['clash_url'] == 'http://sub.example/clash'


def test_exec_apply_empty_payload_preserves_proxy(app):
    app._sync_sticky_from_settings()
    app._exec_apply({'scenario': 'proxy'})
    assert _saved(app)['proxy']['proxies'] == '1.1.1.1:8080'


def test_exec_apply_updates_when_provided(app):
    """带了有效参数时要能更新。"""
    app._sync_sticky_from_settings()
    app._exec_apply({'scenario': 'E', 'api_url': 'http://new-api/x'})
    assert _saved(app)['E']['api_url'] == 'http://new-api/x'


def test_exec_apply_partial_update_keeps_rest(app):
    """只改一个字段，其它字段不能丢。"""
    app._sync_sticky_from_settings()
    app._exec_apply({'scenario': 'F', 'f_mode': 'direct'})
    f = _saved(app)['F']
    assert f['mode'] == 'direct'
    assert f['clash_url'] == 'http://sub.example/clash'


def test_exec_apply_first_time_missing_url_reports_clearly(app):
    """从未配置过、又没带 api_url → 要给出明确提示，不是静默写空。"""
    app._sync_sticky_from_settings()
    s = app.load_settings()
    s['saved_scenarios'] = {}
    app.save_settings(s)

    result = app._exec_apply({'scenario': 'E'})
    assert result.get('ok') is False
    assert 'api_url' in result.get('message', '')
    assert not _saved(app).get('E')      # 不能留下空壳


def test_exec_apply_scenario_a_still_works(app):
    """A 场景（直连）没有关键字段，要照常保存。"""
    _set_sticky(app, False)
    app.deploy_mihomo = lambda cfg, **kw: (True, None)
    app.gen_direct_config = lambda: {'rules': ['MATCH,DIRECT']}
    result = app._exec_apply({'scenario': 'A'})
    assert _saved(app)['A'] == {'configured': True}


# ══ 3. Web 表单入口 ═══════════════════════════════════════════════════

def test_apply_form_empty_preserves_e(app):
    """Web 表单只提交 scenario=E 时，也不能清空 api_url。"""
    app._sync_sticky_from_settings()
    app.api_authorized = lambda: True
    with app.app.test_request_context('/apply', method='POST',
                                      data={'scenario': 'E'}):
        from flask import session as flask_session
        flask_session['authed'] = True      # @login_required 会检查这个
        app.apply()
    assert _saved(app)['E']['api_url'] == 'http://api.example/extract?num=1'


def test_apply_form_with_value_updates(app):
    app._sync_sticky_from_settings()
    app.api_authorized = lambda: True
    with app.app.test_request_context('/apply', method='POST',
                                      data={'scenario': 'E',
                                            'api_url': 'http://form-new/x'}):
        from flask import session as flask_session
        flask_session['authed'] = True
        app.apply()
    assert _saved(app)['E']['api_url'] == 'http://form-new/x'


# ══ 4. 回归：清空后 acquire 必须报错而不是静默用空 URL ═══════════════

def test_acquire_after_wipe_attempt_still_works(app):
    """走一遍「空请求 -> acquire」，api_url 必须还在，acquire 不能报缺 URL。"""
    app._sync_sticky_from_settings()
    app._exec_apply({'scenario': 'E'})

    app._extract_from_api = lambda u, num=1: ['1.1.1.1:1111']
    app._parse_proxy_line = lambda line: {
        'server': line.split(':')[0], 'port': int(line.split(':')[1]), 'type': 'http'}
    app._probe_node_direct = lambda node, timeout=8: True
    app._test_through_port = lambda port, timeout=8: True
    app._reload_sticky = lambda settings=None: (True, None)

    session, err = app.acquire_session('cfg-wipe-regression', scenario='E')
    assert session is not None, err
    assert '请先配置 API URL' not in str(err)


# ══ 5. 默认值不能覆盖已存配置 ═════════════════════════════════════════

def test_exec_apply_keeps_f_mode_poll(app):
    """切 F 时没传 f_mode，不能把 poll 改成 direct。"""
    _set_sticky(app, True)
    app._exec_apply({'scenario': 'F'})
    assert _saved(app)['F']['mode'] == 'poll', _saved(app)['F']


def test_exec_apply_keeps_proxy_type_http(app):
    """切 proxy 时没传 proxy_type，不能把 http 改成 socks5。"""
    _set_sticky(app, True)
    app._exec_apply({'scenario': 'proxy'})
    assert _saved(app)['proxy']['proxy_type'] == 'http', _saved(app)['proxy']


def test_exec_apply_updates_mode_when_given(app):
    _set_sticky(app, True)
    app._exec_apply({'scenario': 'F', 'f_mode': 'direct'})
    assert _saved(app)['F']['mode'] == 'direct'
    assert _saved(app)['F']['clash_url'] == 'http://sub.example/clash'
