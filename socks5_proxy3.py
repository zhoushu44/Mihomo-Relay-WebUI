"""简易 SOCKS5 代理服务器（Python3 版）: 验证粘性故障切换用"""
import socket
import threading
import sys

LISTEN_ADDR = ('0.0.0.0', int(sys.argv[1]) if len(sys.argv) > 1 else 10080)


def handle(client):
    try:
        ver, nmethods = client.recv(2)
        if ver != 5:
            client.close()
            return
        client.recv(nmethods)
        client.sendall(b'\x05\x00')
        hdr = client.recv(4)
        if len(hdr) < 4 or hdr[1] != 1:
            client.close()
            return
        atyp = hdr[3]
        if atyp == 1:
            addr = socket.inet_ntoa(client.recv(4))
        elif atyp == 3:
            ln = client.recv(1)[0]
            addr = client.recv(ln).decode()
        elif atyp == 4:
            addr = socket.inet_ntop(socket.AF_INET6, client.recv(16))
        else:
            client.close()
            return
        port = int.from_bytes(client.recv(2), 'big')
        remote = socket.create_connection((addr, port), timeout=10)
        client.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
        remote.settimeout(30)
        client.settimeout(30)

        def pump(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    b.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=pump, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=pump, args=(remote, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN_ADDR)
    srv.listen(128)
    print(f'SOCKS5 proxy listening on {LISTEN_ADDR[0]}:{LISTEN_ADDR[1]}', flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == '__main__':
    main()
