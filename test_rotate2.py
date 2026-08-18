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

def sess(d, tid):
    for s in d.get('sessions') or []:
        if s.get('task_id') == tid:
            return s
    return None

st, d = q('GET', f'/api/v1/proxy?key={KEY}&session=rot_b')
print('acquire rot_b:', d.get('proxy'))
st, d = q('GET', f'/api/ui/bootstrap?key={KEY}')
s1 = sess(d, 'rot_b')
print('轮换前 内部 proxy =', s1.get('proxy') if s1 else None, '| listener =', s1.get('listener') if s1 else None)

st, d = q('POST', '/api/ui/sticky-rotate', {'task_id': 'rot_b'}, timeout=120)
print('rotate  ->', st, d.get('ok'), d.get('message'))

st, d = q('GET', f'/api/ui/bootstrap?key={KEY}')
s2 = sess(d, 'rot_b')
if s2:
    print('轮换后 内部 proxy =', s2.get('proxy'), '| listener =', s2.get('listener'))
    print('后端已切换(PASS):', s1.get('proxy') != s2.get('proxy') or 'IP同但ref换?')
    st, d = q('GET', f'/api/v1/proxy/destroy?key={KEY}&session=rot_b')
    print('destroy rot_b ->', st, d.get('ok'))