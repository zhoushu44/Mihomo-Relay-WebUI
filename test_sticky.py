# -*- coding: utf-8 -*-
import json, urllib.request, urllib.error

BASE = 'http://192.6.121.16:7892'
KEY = 'full-test-api-key'

def q(method, path, body=None, key=KEY, timeout=90):
    h = {'Content-Type': 'application/json'}
    if key: h['X-API-Key'] = key
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, json.loads(resp.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        t = e.read().decode('utf-8', 'replace')
        try: return e.code, json.loads(t)
        except Exception: return e.code, t

def proxy_of(d):
    if not isinstance(d, dict): return None
    p = d.get('proxy')
    if isinstance(p, dict): return p.get('proxy') or p
    c = d.get('connections') or {}
    return (c.get('socks') or c.get('http') or {})

print('== 粘性隔离性：不同 session 应得到不同 IP ==')
s, d1 = q('GET', f'/api/v1/proxy?key={KEY}&session=iso_aaa')
p1 = proxy_of(d1)
s, d2 = q('GET', f'/api/v1/proxy?key={KEY}&session=iso_bbb')
p2 = proxy_of(d2)
print(f'  session=iso_aaa -> {s} {str(p1)[:80]}')
print(f'  session=iso_bbb -> {s} {str(p2)[:80]}')
u1, u2 = str(p1), str(p2)
print('  隔离性(PASS 两者不同):', u1 != u2 and u1 != 'None' and u2 != 'None')
if u1 != u2:
    # 同 session 再取一次验证仍未变（幂等）
    s, d3 = q('GET', f'/api/v1/proxy?key={KEY}&session=iso_aaa')
    p3 = proxy_of(d3)
    print('  同 session 复取仍同 IP(PASS):', str(p3) == u1, str(p3)[:60])

print('== 销毁两个隔离会话 ==')
for sid in ('iso_aaa', 'iso_bbb'):
    st, d = q('GET', f'/api/v1/proxy/destroy?key={KEY}&session={sid}')
    print(f'  destroy {sid} -> {st} ok={d.get("ok") if isinstance(d, dict) else "?"}')

print('== 轮换 rotate ==')
st, d = q('POST', '/api/ui/sticky-rotate', {}, timeout=120)
print('  rotate ->', st, json.dumps(d, ensure_ascii=False)[:200])

print('== 会话状态（bootstrap 中 sticky） ==')
st, d = q('GET', f'/api/ui/bootstrap?key={KEY}')
sd = d.get('sticky') or {}
print('  sticky.enabled =', sd.get('enabled'), '| sessions 数 =', len(d.get('sessions') or []))