"""补充验证：A1(总开关+acquire/release) A2(B/C/D轮询分配同IP多任务)
A12(B/C/D故障切换端口不变) A13(热重载不重启容器) A17(API端点) + 恢复现场"""
import json, sys, time
sys.path.insert(0, '.')
from test_common import (run, set_settings, acquire, release, status, slist, pool,
                         restart_web, start_proxies, kill_proxy, P)

out, _ = run('cat /tmp/terminal_settings.json')
ORIG = json.loads(out)

b = dict(ORIG)
b['scenario'] = 'proxy'
b['saved_scenarios'] = {
    'proxy': {'proxy_type': 'socks5', 'proxies': '172.17.0.2:10080\n172.17.0.2:10081',
              'username': '', 'password': '', 'rotate': 'yes'},
    'E': ORIG.get('saved_scenarios', {}).get('E', {}),
}
b['sticky'] = {'enabled': True, 'test_url': 'http://223.5.5.5/', 'test_enabled': True,
               'timeout': 600, 'queue_timeout': 30}
set_settings(b)
restart_web()
start_proxies()

# ============ A1: 总开关 + acquire/release 正常 ============
print('===== A1 总开关+acquire/release =====')
r = acquire('task_a1')
s1 = r.get('session', {})
P('A1 粘性开启 acquire 正常', bool(r.get('ok')) and 40001 <= s1.get('listener_port', 0) <= 40999,
  json.dumps(s1, ensure_ascii=False)[:200])
r = release('task_a1')
P('A1 release 正常', r.get('ok'), json.dumps(r, ensure_ascii=False)[:150])

# ============ A13: 热重载不重启容器 ============
print('===== A13 热重载不重启容器 =====')
out, _ = run("docker inspect mihomo --format '{{.State.StartedAt}}'")
sb = out.strip()
r1 = acquire('task_a13')
r2 = release('task_a13')
out, _ = run("docker inspect mihomo --format '{{.State.StartedAt}}'")
sa = out.strip()
P('A13 热重载不重启容器', bool(r1.get('ok')) and bool(r2.get('ok')) and sb == sa,
  f'StartedAt={sb}')

# ============ A2: B/C/D 轮询分配，同一IP可多任务共用 ============
print('===== A2 B/C/D 轮询分配+复用 =====')
ids = []
ok_all = True
for i in range(4):
    r = acquire(f'task_a2_{i}')
    ok_all = ok_all and bool(r.get('ok'))
    ids.append(r.get('session', {}).get('proxy'))
    time.sleep(0.5)
lst = slist()
n = len(lst.get('sessions', []))
dup = len(ids) != len(set(ids))
P('A2 B/C/D 轮询分配+同IP多任务复用', bool(ok_all) and n == 4 and dup,
  f'proxies={ids} sessions={n}')
for i in range(4):
    release(f'task_a2_{i}')

# ============ A17: API 端点 ============
print('===== A17 API 端点 =====')
ok_api = True
r = acquire('task_a17'); ok_api = ok_api and bool(r.get('ok'))
r = status('task_a17'); ok_api = ok_api and bool(r.get('ok')) and r.get('session', {}).get('task_id') == 'task_a17'
r = slist(); ok_api = ok_api and bool(r.get('ok')) and any(x.get('task_id') == 'task_a17' for x in r.get('sessions', []))
r = pool(); ok_api = ok_api and bool(r.get('ok')) and 'pool' in r
r = release('task_a17'); ok_api = ok_api and bool(r.get('ok'))
P('A17 acquire/release/status/list/pool API', ok_api)

# ============ A12: B/C/D 故障自动切换（端口不变） ============
print('===== A12 B/C/D 故障切换端口不变 =====')
r = acquire('task_a12')
s = r.get('session', {})
port = s.get('listener_port'); proxy = s.get('proxy')
print(f'  绑定: port={port} proxy={proxy}')
if not port:
    print('  acquire 失败，跳过 A12:', json.dumps(r, ensure_ascii=False)[:300])
else:
    kill_port = int(proxy.rsplit(':', 1)[-1])
    kill_proxy(kill_port)
    switched = False
    for i in range(12):
        time.sleep(10)
        s2 = status('task_a12').get('session', {})
        if s2.get('proxy') != proxy and s2.get('listener_port') == port:
            switched = True
            print(f'  t+{(i+1)*10}s 切换: proxy={s2.get("proxy")} port={s2.get("listener_port")}')
            break
    P('A12 B/C/D 故障切换端口不变', switched, json.dumps(s2, ensure_ascii=False))
    release('task_a12')

# ============ 恢复现场 ============
print('===== 恢复原始配置 =====')
set_settings(ORIG)
restart_web()
start_proxies()
r = pool()
print('恢复后 pool:', json.dumps(r, ensure_ascii=False)[:200])
print('=== verify_missing 完成 ===')
