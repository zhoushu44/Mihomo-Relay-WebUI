"""Phase 3: A16(排队30s超时) A18(WebUI) A19(测试URL配置) A20(关闭粘性原功能不受影响) + 恢复现场"""
import json, sys, time
sys.path.insert(0, '.')
from test_common import (run, set_settings, acquire, release, status, slist, pool,
                         restart_web, start_proxies, kill_proxy, P, WEB, KEY)

out, _ = run('cat /tmp/terminal_settings.json')
ORIG = json.loads(out)  # 备份原始配置

# ==================== A16: 排队30秒超时 ====================
print('===== A16 排队超时 =====')
b = dict(ORIG)
b['scenario'] = 'proxy'
b['sticky'] = {'enabled': True, 'test_url': 'http://223.5.5.5/', 'test_enabled': True,
               'timeout': 600, 'queue_timeout': 30}
set_settings(b)
restart_web()
start_proxies()

kill_proxy(10080)
kill_proxy(10081)
r = acquire('task_q1')
print('  q1 ->', r.get('error', 'ok'))
t0 = time.time()
r = acquire('task_q2')
err = r.get('error', '')
elapsed = time.time() - t0
P('A16 排队30秒超时', not r.get('ok') and '排队超时' in err and elapsed >= 25,
  f'error={err} 耗时={elapsed:.0f}s')

# ==================== A18/A19: WebUI（粘性开启 + 活跃会话） ====================
print('===== A18/A19 WebUI =====')
restart_web()  # 清内存池状态（A16 中节点被标记不可用）
start_proxies()  # 恢复代理进程
r = acquire('task_ui')
print('  active ->', r.get('port', r.get('error')))
assert r.get('ok'), f'预置活跃会话失败: {r}'
# 有活跃会话后抓取 WebUI 页面（此时粘性已开启，释放/切换按钮条件渲染可见）
out, _ = run("curl -sS -c /tmp/ck.txt -d 'pwd=mihomo123' http://127.0.0.1:7892/login -o /dev/null -w '%{http_code}'")
print('  login http_code:', out.strip())
out, _ = run("curl -sS -b /tmp/ck.txt http://127.0.0.1:7892/")
html = out
ok_ui = ('粘性' in html) and ('test_url' in html or 'testUrl' in html) and ('释放' in html or 'release' in html)
P('A18/A19 WebUI 粘性面板+测试URL+释放', ok_ui, f'页面长度={len(html)}')
release('task_ui')

# ==================== A20: 关闭粘性模式 ====================
print('===== A20 关闭粘性 =====')
b['sticky']['enabled'] = False
set_settings(b)
restart_web()
start_proxies()  # 恢复代理，保证轮换模式可用

r = acquire('task_a20')
P('A20 关闭后 acquire 拒绝', not r.get('ok') and '未开启' in r.get('error', ''), f'error={r.get("error")}')

# 7890 原轮换代理仍工作（绕 socket 认证；密码含 @ 需 URL 编码 %401）
out, _ = run("curl -sS -x socks5h://sockstest:socks-pass%401@127.0.0.1:7890 --connect-timeout 5 --max-time 10 "
             "-o /dev/null -w '%{http_code}' http://223.5.5.5/")
ok_7890 = out.strip().isdigit() and 0 < int(out.strip()) < 500
P('A20 关闭后 7890 轮换仍可用', ok_7890, f'http_code={out.strip()}')

out, _ = run("grep -A3 'proxy-groups' /tmp/mihomo_config.yaml | head -6")
P('A20 mihomo 配置含轮换组', 'rotate' in out, out.strip().replace('\n', ' | ')[:200])

# ==================== 恢复现场 ====================
print('===== 恢复原始配置 =====')
set_settings(ORIG)
restart_web()
start_proxies()
r = pool()
print('恢复后 pool:', json.dumps(r, ensure_ascii=False)[:300])
out, _ = run("curl -sS -x socks5h://sockstest:socks-pass%401@127.0.0.1:7890 --connect-timeout 5 --max-time 10 "
             "-o /dev/null -w '%{http_code}' http://223.5.5.5/")
print('恢复后 7890 http_code:', out.strip())
print('=== Phase 3 完成 ===')
