import hashlib
from ssh_helper import run_remote

def md5(p):
    return hashlib.md5(open(p, 'rb').read()).hexdigest()

local = {
    'app.py': md5('app.py'),
    'static/index.html': md5('static/index.html'),
}
import glob, os
for f in glob.glob('static/assets/*'):
    f = f.replace('\\', '/')
    local[f] = md5(f)
print('本地 md5:', {k: v[:8] for k, v in local.items()})

cmds = []
for p in list(local.keys()):
    cmds.append(f"md5sum /app/{p} 2>/dev/null || echo 'MISSING /app/{p}'")
out = run_remote(' && '.join(['docker exec mihomo-web sh -c "' + '; '.join(cmds) + '"']))
out = out[0] if isinstance(out, tuple) else out
print(out)

import re
remotes = {}
for line in out.splitlines():
    m = re.match(r'([0-9a-f]{32})\s+(/[^ ]+)$', line.strip())
    if m:
        remotes[m.group(2).replace('/app/', '')] = m.group(1)
    elif 'MISSING' in line:
        path = line.split('MISSING ')[-1].strip('/app/')
        remotes[path] = None

all_ok = True
for k, v in local.items():
    r = remotes.get(k)
    status = f'{r[:8]}' if r else '缺失'
    ok = r == v
    all_ok = all_ok and ok
    print(f'  {k}: 本地={v[:8]} 容器={status} {"一致 ✓" if ok else "不一致 ✗"}')
print('\n总体:', '全部一致 ✓' if all_ok else '存在不一致 ✗')