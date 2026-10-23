"""在隔离命名空间内使用原生 app-server 读取配置与 Hook 信任标识。"""
from __future__ import annotations

import json
import subprocess
import sys


def main():
    with subprocess.Popen(["/opt/hp/codex", "app-server"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as proc:
        def request(ident, method, params):
            proc.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            while line := proc.stdout.readline():
                value = json.loads(line)
                if value.get("id") == ident:
                    if "error" in value:
                        raise RuntimeError(json.dumps(value["error"]))
                    return value["result"]
            raise RuntimeError("app-server 提前退出")

        try:
            request(1, "initialize", {"clientInfo": {"name": "harnesspilot", "version": "0.2.0"},
                                      "capabilities": {"experimentalApi": True}})
            proc.stdin.write('{"method":"initialized"}\n')
            proc.stdin.flush()
            result = {"hooks": request(2, "hooks/list", {"cwds": ["/workspace"]}),
                      "config": request(3, "config/read", {"cwd": "/workspace"})}
            if "--start" in sys.argv:
                result["thread"] = request(4, "thread/start", {
                    "cwd": "/workspace", "approvalPolicy": "never", "sandbox": "workspace-write",
                    "ephemeral": True})
            print(json.dumps(result))
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    main()
