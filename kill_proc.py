#!/usr/bin/env python3
"""容器内按 cmdline 匹配并 kill 进程：kill_proc.py <pattern> [port]"""
import os, sys

pat = sys.argv[1]
if len(sys.argv) > 2:
    pat = pat + ' ' + sys.argv[2]

self_pid = os.getpid()
killed = []
for p in os.listdir('/proc'):
    if not p.isdigit():
        continue
    if int(p) == self_pid:
        continue
    try:
        with open(f'/proc/{p}/cmdline', 'rb') as f:
            cmd = f.read().replace(b'\0', b' ').decode(errors='ignore')
    except Exception:
        continue
    if pat in cmd:
        try:
            os.kill(int(p), 15)
            killed.append(p)
        except Exception:
            pass
print('killed:', killed)
