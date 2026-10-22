"""Private loopback proxy inside a network namespace; no external network."""
import os
import select
import socket
import socketserver
import sys
import threading

class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        remote = socket.socket(socket.AF_UNIX)
        try:
            remote.connect("/broker/api.sock")
            active = [self.request, remote]
            while True:
                ready, _, _ = select.select(active, [], [], 200)
                if not ready: return
                for source in ready:
                    data = source.recv(65536)
                    if not data: return
                    (remote if source is self.request else self.request).sendall(data)
        finally: remote.close()

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

server = Server(("127.0.0.1", 17280), Relay)
threading.Thread(target=server.serve_forever, daemon=True).start()
# exec would kill the relay thread, so wait for Codex as a child instead.
import subprocess
raise SystemExit(subprocess.call(sys.argv[1:]))
