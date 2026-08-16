"""Phase 2: 场景 F 测试
直连: A6/A7(绑定第一可用+不过期+故障切换端口不变)/A9(3次失败排除)/A10(全部不可用)
轮询: A8(轮询分配+跳过+过期)/A9-poll(排除)/A11(复用)/A15(过期清理)
"""
import json, sys, time
sys.path.insert(0, '.')
from test_common import (run, set_settings, acquire, release, status, slist,
                         restart_web, start_proxies, start_mock, kill_proxy, P)

out, _ = run('cat /tmp/terminal_settings.json')
base = json.loads(out)


def set_f(mode, timeout, test_enabled=True):
    b = dict(base)
    b['scenario'] = 'F'
    b['saved_scenarios'] = {
        'F': {'clash_url': 'http://127.0.0.1:10099/mock.yaml', 'mode': mode},
        'proxy': base.get('saved_scenarios', {}).get('proxy', {}),
    }
    b['sticky'] = {'enabled': True, 'test_url': 'http://223.5.5.5/',
                   'test_enabled': test_enabled, 'timeout': timeout, 'queue_timeout': 30}
    set_settings(b)
    restart_web()          # 重启确保内存同步（f_mode/test_enabled/timeout）
    start_proxies()
    start_mock()


# ==================== F 直连模式 ====================
print('===== F 直连模式 =====')
set_f('direct', 600, True)

# A7a: 绑定第一个可用节点 + 不过期
r = acquire('task_f_1')
s = r.get('session') or {}
ok = r.get('ok') and s.get('proxy') == '172.17.0.2:10080' and s.get('listener_port') == 40001 \
     and s.get('expires_at') is None and s.get('scenario') == 'F'
P('A6/A7 F直连绑定第一可用+不过期', bool(ok), json.dumps(s, ensure_ascii=False))
if not ok:
    print('raw:', json.dumps(r, ensure_ascii=False)[:500])
    sys.exit(1)

# A7b: 故障自动切换，端口不变
kill_proxy(10080)
print('已 kill 10080，轮询等待健康检查切换(最长120s)...')
switched = False
for i in range(12):
    time.sleep(10)
    s = status('task_f_1').get('session') or {}
    if s.get('proxy') == '172.17.0.2:10081' and s.get('listener_port') == 40001:
        switched = True
        print(f'  第{(i+1)*10}s 切换完成:', json.dumps(s, ensure_ascii=False))
        break
P('A7 F直连故障自动切换(端口不变)', switched, json.dumps(s, ensure_ascii=False))

release('task_f_1')

# A10: 全部不可用 → 失败（此时 10080 已死，再杀 10081）
kill_proxy(10081)
r = acquire('task_f_a')
err = r.get('error', '')
P('A10 F直连全部不可用返回失败', not r.get('ok') and '均不可用' in err, f'error={err}')

# A9: 连续失败3次排除（再 acquire 两次凑满计数，然后复活节点验证排除）
for i, tid in enumerate(['task_f_b', 'task_f_c']):
    r = acquire(tid)
    print(f'  {tid} ->', r.get('error', 'ok'))

start_proxies()  # 复活 10080/10081
print('节点已复活，但失败计数应已=3，再次 acquire 应仍失败...')
r = acquire('task_f_d')
err = r.get('error', '')
P('A9 F直连连续失败3次排除', not r.get('ok') and '均不可用' in err, f'error={err}')

# ==================== F 轮询模式 ====================
print('===== F 轮询模式（重启清状态）=====')
restart_web()
start_proxies()
start_mock()
set_f('poll', 45, True)

# A8: 轮询分配 + 过期时间
p1 = acquire('task_p1').get('session') or {}
p2 = acquire('task_p2').get('session') or {}
p3 = acquire('task_p3').get('session') or {}
P('A8 F轮询节点1', p1.get('proxy') == '172.17.0.2:10080' and p1.get('listener_port') == 40001 and p1.get('expires_at'), json.dumps(p1, ensure_ascii=False))
P('A8 F轮询节点2(轮询分配)', p2.get('proxy') == '172.17.0.2:10081' and p2.get('listener_port') == 40002, json.dumps(p2, ensure_ascii=False))
P('A8 F轮询节点1(回绕)', p3.get('proxy') == '172.17.0.2:10080' and p3.get('listener_port') == 40003, json.dumps(p3, ensure_ascii=False))

# A8: 不可用跳过 — kill 10080 后 acquire 应跳过 n1
kill_proxy(10080)
p4 = acquire('task_p4').get('session') or {}
P('A8 F轮询不可用跳过', p4.get('proxy') == '172.17.0.2:10081' and p4.get('listener_port') == 40004, json.dumps(p4, ensure_ascii=False))

# A9-poll: 连续3次命中失败节点后排除 — 依次 acquire，前几次轮询到 n1 会失败并计数
p5 = acquire('task_p5').get('session') or {}
p6 = acquire('task_p6').get('session') or {}
p7 = acquire('task_p7').get('session') or {}
print('p5:', p5.get('proxy'), 'p6:', p6.get('proxy'), 'p7:', p7.get('proxy'))

start_proxies()  # 复活 10080
p8 = acquire('task_p8').get('session') or {}
ok_excl = p8.get('proxy') == '172.17.0.2:10081'  # n1 已被排除，必须绑 n2
P('A9 F轮询连续失败3次排除', ok_excl, f'复活后 acquire 绑定 {p8.get("proxy")}（应为 10081）')

# A11: 节点不够时复用
for tid in [f'task_p{i}' for i in range(1, 9)]:
    release(tid)
kill_proxy(10080)
kill_proxy(10081)
r = acquire('task_p9')
s = r.get('session') or {}
P('A11 F轮询节点不够复用', bool(r.get('ok')), json.dumps(s, ensure_ascii=False))

# A15: F轮询过期清理（timeout=45）
print('等待 100s 过期清理...')
time.sleep(100)
lst = slist()
ids = [x['task_id'] for x in lst.get('sessions', [])]
P('A15 F轮询过期自动清理', 'task_p9' not in ids, f'sessions={ids}')

print('=== Phase 2 完成 ===')
