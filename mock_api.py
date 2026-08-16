#!/usr/bin/env python3
"""Mock E 提取 API + F Clash 订阅（容器内运行，监听 argv[1] 端口）"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MOCK_YAML = """proxies:
  - name: n1
    type: socks5
    server: 172.17.0.2
    port: 10080
  - name: n2
    type: socks5
    server: 172.17.0.2
    port: 10081
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == '/api':
            n = int(parse_qs(u.query).get('num', ['1'])[0])
            body = '\n'.join('socks5://172.17.0.2:%d' % (10080 + (i % 2)) for i in range(n))
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(body.encode())
        elif p == '/fail':
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'error')
        elif p == '/badproxy':
            body = 'socks5://10.255.255.1:9'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(body.encode())
        elif p == '/mock.yaml':
            self.send_response(200)
            self.send_header('Content-Type', 'text/yaml')
            self.end_headers()
            self.wfile.write(MOCK_YAML.encode())
        else:
            self.send_response(404)
            self.end_headers()


port = int(sys.argv[1])
ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
