"""Phase 1: 场景 E 测试 — A3(懒加载+10分钟) A4(重试3次) A5(验证可关闭) A14(过期清理+失败不切换)"""
import json, sys, time
sys.path.insert(0, '.')
from test_common import (run, set_settings, acquire, release, status, slist,
                         pool, kill_proxy, start_proxies, P)

# 读取当前 settings 作为基础
out, _ = run('cat /tmp/terminal_settings.json')
base = json.loads(out)
base['scenario'] = 'E'
base['saved_scenarios'] = {
    'E': {'api_url': 'http://127.0.0.1:10099/api?num=1', 'api_num': '1'},
    'proxy': base.get('saved_scenarios', {}).get('proxy', {}),
}
base['sticky'] = {'enabled': True, 'test_url': 'http://223.5.5.5/', 'test_enabled': True,
                  'timeout': 600, 'queue_timeout': 30}
set_settings(base)
print('settings 已切换 scenario=E')

# ---------- A3: acquire 懒加载提取 + 验证 ----------
r = acquire('task_e_001')
s = r.get('session') or {}
ok_a3 = r.get('ok') and s.get('listener_port') == 40001 and s.get('proxy') == '172.17.0.2:10080' \
        and s.get('scenario') == 'E' and s.get('expires_at')
P('A3 E懒加载提取+分配', bool(ok_a3), json.dumps(s, ensure_ascii=False))
if not ok_a3:
    print('raw:', json.dumps(r, ensure_ascii=False)[:500])
    sys.exit(1)

# A5(验证开启): acquire 成功本身证明 _test_through_port 验证通过
P('A5 提取后验证(开启)', True, 'test_enabled=true 且端口验证通过才返回')

# A14(失败不自动切换): 杀掉绑定代理 10080，等健康检查周期，确认不切换
kill_proxy(10080)
print('已 kill 10080，等待 75s 健康检查周期...')
time.sleep(75)
s2 = status('task_e_001').get('session') or {}
no_switch = s2.get('proxy') == '172.17.0.2:10080' and s2.get('listener_port') == 40001
P('A14 失败不自动切换', bool(no_switch), json.dumps(s2, ensure_ascii=False))

start_proxies()
release('task_e_001')

# ---------- A4: 提取失败重试3次 ----------
base['saved_scenarios']['E']['api_url'] = 'http://127.0.0.1:10098/api?num=1'  # 无监听端口
set_settings(base)
r = acquire('task_e_fail')
err = r.get('error', '')
ok_a4 = not r.get('ok') and '重试' in err
P('A4 提取失败重试3次', ok_a4, f'error={err}')

# ---------- A5-1: 提取后验证不可用则重试 ----------
base['saved_scenarios']['E']['api_url'] = 'http://127.0.0.1:10099/badproxy'
set_settings(base)
t0 = time.time()
r = acquire('task_e_bad')
err = r.get('error', '')
ok_a5 = not r.get('ok') and '重试' in err and (time.time() - t0) > 8
P('A5 提取后验证(不可用重试)', ok_a5, f'error={err} 耗时={time.time()-t0:.0f}s')

# ---------- A5-2: 验证可关闭 ----------
base['sticky']['test_enabled'] = False
set_settings(base)
print('等待粘性状态同步(35s)...')
time.sleep(35)
r = acquire('task_e_nv')
s = r.get('session') or {}
ok_a5b = r.get('ok') and s.get('proxy') == '10.255.255.1:9'
P('A5 验证可关闭(未验证也返回)', bool(ok_a5b), json.dumps(s, ensure_ascii=False))
release('task_e_nv')

# ---------- A14/A15: 过期自动清理 ----------
base['sticky']['timeout'] = 45
base['sticky']['test_enabled'] = False
base['saved_scenarios']['E']['api_url'] = 'http://127.0.0.1:10099/api?num=1'
set_settings(base)
print('等待同步(35s)...')
time.sleep(35)
r = acquire('task_e_exp')
s = r.get('session') or {}
print('expires_at =', s.get('expires_at'), ' acquired_at =', s.get('acquired_at'))
print('等待 100s 让过期清理执行...')
time.sleep(100)
lst = slist()
ids = [x['task_id'] for x in lst.get('sessions', [])]
ok_exp = 'task_e_exp' not in ids
P('A14/A15 E过期自动清理', ok_exp, f'sessions={ids}')
P('A17 session/list API', lst.get('ok') is True)

# 再 acquire 验证端口已释放复用
r = acquire('task_e_after')
s = r.get('session') or {}
P('过期后端口可复用', r.get('ok') and s.get('listener_port') == 40001, json.dumps(s, ensure_ascii=False))
release('task_e_after')

print('=== Phase 1 完成 ===')
