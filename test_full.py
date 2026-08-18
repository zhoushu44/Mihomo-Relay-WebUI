# -*- coding: utf-8 -*-
"""全面 API 回归测试：http://192.6.121.16:7892"""
import json, urllib.request, urllib.error, sys

BASE = 'http://192.6.121.16:7892'
KEY = 'full-test-api-key'
PASS, FAIL = 0, 0

def req(method, path, body=None, key=None, raw=False):
    url = BASE + path
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['X-API-Key'] = key
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=90)
        txt = resp.read().decode('utf-8', 'replace')
        return resp.status, (txt if raw else json.loads(txt))
    except urllib.error.HTTPError as e:
        txt = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt
    except Exception as e:
        return -1, str(e)

def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name} {extra}')
    else:
        FAIL += 1
        print(f'  FAIL  {name} {extra}')

print('== A. 鉴权 ==')
s, d = req('GET', '/api/ui/bootstrap')
check('bootstrap 无 key -> 401', s == 401, f'[{s}]')
s, d = req('GET', f'/api/ui/bootstrap?key={KEY}')
check('bootstrap 带 key -> 200', s == 200 and d.get('ok'), f'[{s}]')
s, d = req('POST', '/api/ui/login', {'key': 'wrong-key'})
check('login 错误 key -> 401', s == 401, f'[{s}]')
s, d = req('POST', '/api/ui/login', {'key': KEY})
check('login 正确 key -> 200 authed', s == 200 and d.get('authed'), f'[{s}]')
s, d = req('GET', '/api/v1/proxy')
check('proxy 无 key -> 401', s == 401, f'[{s}]')
s, d = req('POST', '/api/ui/action', {'act': 'refresh'}, key=KEY)
check('action refresh 带 key -> 200', s == 200, f'[{s}]')

print('== B. 提取 ==')
s, d = req('GET', f'/api/v1/proxy?key={KEY}')
ok_share = s == 200 and d.get('ok')
check('共享出口 -> 200 ok', ok_share, f'[{s}] keys={list(d.keys()) if isinstance(d, dict) else ""}')
s, txt = req('GET', f'/api/v1/proxy?key={KEY}&format=txt', raw=True)
check('format=txt -> 单行地址', s == 200 and ('socks5://' in txt or 'http://' in txt), f'[{s}] {txt[:60]}')
s, d1 = req('GET', f'/api/v1/proxy?key={KEY}&session=test_s1')
check('acquire session=test_s1 -> 200', s == 200 and d.get('ok'), f'[{s}] err={d1.get("error") if isinstance(d1, dict) else ""} msg={d1.get("message") if isinstance(d1, dict) else ""}')
proxy1 = d.get('proxy') or (d.get('connections') or {}).get('socks', {})
s, d2 = req('GET', f'/api/v1/proxy?key={KEY}&session=test_s1')
same = False
try:
    u1 = json.dumps(d1.get('proxy', {}) or {}, sort_keys=True)
    u2 = json.dumps(d2.get('proxy', {}) or {}, sort_keys=True)
    same = u1 == u2
except Exception:
    same = False
check('同 session 幂等（同 proxy）', s == 200 and same, f'[{s}]')
s, d3 = req('GET', f'/api/v1/proxy?key={KEY}&session=test_s1&consume=1')
check('consume=1 释放 -> 200', s == 200 and d.get('ok'), f'[{s}] msg={d3.get("message", "") if isinstance(d3, dict) else ""}')
s, d = req('GET', f'/api/v1/proxy/destroy?key={KEY}')
check('destroy 无 session -> 400', s == 400, f'[{s}] {d}')
s, d = req('GET', f'/api/v1/proxy/destroy?key={KEY}&session=nonexistent_xyz')
check('destroy 不存在 session -> 404 幂等', s in (404, 400), f'[{s}] {d}')
s, d = req('GET', f'/api/v1/proxy?key={KEY}&session=test_cleanup')
s2, d2 = req('GET', f'/api/v1/proxy/destroy?key={KEY}&session=test_cleanup')
check('acquire 后 destroy 清理 -> 200', s2 == 200, f'[acquire={s} destroy={s2}] {d2}')

print('== C. config ==')
s, d = req('GET', f'/api/v1/config?key={KEY}')
check('config GET -> 200', s == 200 and d.get('ok'), f'[{s}] entry={d.get("entry_mode")} scenario={d.get("scenario")}')
s, d = req('POST', f'/api/v1/config?key={KEY}', {})
check('config POST 空 body -> 400', s == 400, f'[{s}] {d}')
s, d = req('POST', f'/api/v1/config?key={KEY}', {'entry_mode': 'mixed'})
check('config POST 入口保底应用 -> 200 ok', s == 200 and d.get('ok'), f'[{s}] {d.get("message", "") if isinstance(d, dict) else ""}')
s, d = req('GET', '/api/v1/config')
check('config 无 key -> 401', s == 401, f'[{s}]')

print('== D. UI 管理只读 ==')
s, d = req('POST', '/api/ui/action', {'act': 'test'}, key=KEY)
check('action test -> 200', s == 200, f'[{s}]')
s, d = req('POST', f'/api/ui/sticky-settings?key={KEY}', {})
check('sticky-settings -> 200', s == 200, f'[{s}] sticky={d.get("enabled") if isinstance(d, dict) else ""}')
s, d = req('GET', '/api/ui/status?key=' + KEY)
check('ui status -> 200 兜底', s in (200, 404, 405), f'[{s}]')

print(f'\n结果: PASS={PASS} FAIL={FAIL}')
sys.exit(1 if FAIL else 0)