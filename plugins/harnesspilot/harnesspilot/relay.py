"""沙箱内部的 TCP/Unix 转接器；无上游地址、认证与预算控制权。"""
from __future__ import annotations

import select
import socket
import socketserver
import subprocess
import sys
import threading


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as peer:
            peer.connect("/run/model.sock")
            sockets = [peer, self.request]
            while True:
                ready, _, _ = select.select(sockets, [], [], 120)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (peer if source is self.request else self.request).sendall(data)


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    with Server(("127.0.0.1", 19876), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return subprocess.call(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
