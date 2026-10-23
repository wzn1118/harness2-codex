from __future__ import annotations

import hmac
import http.server
import json
import os
import secrets
import socketserver
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .budget import Budget, BudgetExceeded
from .config import ModelConfig


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("模型代理拒绝重定向")


def validate_request(data: dict, cfg: ModelConfig, compact: bool = False):
    if data.get("model") != cfg.model:
        raise ValueError("模型与已批准配置不一致")
    if not compact and data.get("reasoning", {}).get("effort") != cfg.effort:
        raise ValueError("推理设置与已批准配置不一致")
    if data.get("background") or data.get("previous_response_id"):
        raise ValueError("拒绝后台请求与远端会话引用")

    def visit(value):
        if isinstance(value, dict):
            for name, item in value.items():
                if name in {"image_url", "file_url"} and isinstance(item, str):
                    if not item.startswith("data:"):
                        raise ValueError("拒绝供应商侧远程文件读取")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    def tool(value):
        if not isinstance(value, dict) or value.get("type") not in {
                "function", "custom", "namespace"}:
            raise ValueError("模型代理仅允许本地工具定义")
        if value.get("type") == "namespace":
            for child in value.get("tools", []):
                tool(child)

    for value in data.get("tools", []):
        tool(value)
    visit(data)


class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


class Broker:
    def __init__(self, path: Path, config: ModelConfig, budget: Budget, *, opener=None,
                 socket_uid: int | None = None, socket_gid: int | None = None):
        self.path, self.config, self.budget = path, config, budget
        self.socket_uid = os.getuid() if socket_uid is None else socket_uid
        self.socket_gid = os.getgid() if socket_gid is None else socket_gid
        self.token = secrets.token_urlsafe(32)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        self.server = None

    def __enter__(self):
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_error(405)

            def do_POST(self):
                ticket = None
                usage = None
                status = "failed"
                sent = False
                try:
                    if not hmac.compare_digest(self.headers.get("Authorization", ""),
                                               "Bearer " + broker.token):
                        raise ValueError("本地代理认证失败")
                    if self.path not in {"/v1/responses", "/v1/responses/compact"}:
                        raise ValueError("模型代理路径未获授权")
                    if self.headers.get("Transfer-Encoding"):
                        raise ValueError("模型请求必须提供固定长度")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4_194_304:
                        raise ValueError("模型请求长度超出限制")
                    self.connection.settimeout(120)
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError("模型请求不完整")
                    data = json.loads(body)
                    compact = self.path.endswith("/compact")
                    validate_request(data, broker.config, compact)
                    if not compact:
                        requested = data.get("max_output_tokens", broker.budget.limits.output_tokens)
                        if type(requested) is not int or not 1 <= requested <= broker.budget.limits.output_tokens:
                            raise ValueError("请求输出额度超出已批准上限")
                        data["max_output_tokens"] = requested
                        data["store"] = False
                    body = json.dumps(data, ensure_ascii=False).encode()
                    ticket = broker.budget.begin(len(body))
                    target = broker.config.base_url + self.path.removeprefix("/v1")
                    if broker.config.query:
                        target += "?" + urllib.parse.urlencode(broker.config.query)
                    headers = {**broker.config.headers, "Content-Type": "application/json",
                               "Authorization": "Bearer " + broker.config.key}
                    request = urllib.request.Request(target, body, headers, method="POST")
                    with broker.opener.open(request, timeout=120) as response:
                        content_type = response.headers.get("Content-Type", "application/json")
                        self.send_response(response.status)
                        self.send_header("Content-Type", content_type)
                        self.end_headers()
                        sent = True
                        total = 0
                        if "text/event-stream" in content_type:
                            while line := response.readline(4_194_305):
                                broker.budget.check()
                                total += len(line)
                                if total > 16_777_216 or len(line) > 4_194_304:
                                    raise ValueError("模型流超出长度限制")
                                if line.startswith(b"data:"):
                                    try:
                                        event = json.loads(line[5:])
                                        usage = event.get("response", {}).get("usage") or event.get("usage") or usage
                                    except (ValueError, AttributeError):
                                        pass
                                self.wfile.write(line)
                                self.wfile.flush()
                        else:
                            result = response.read(16_777_217)
                            if len(result) > 16_777_216:
                                raise ValueError("模型响应超出长度限制")
                            usage = json.loads(result).get("usage")
                            self.wfile.write(result)
                        status = "ok"
                except (ValueError, TypeError, AttributeError, BudgetExceeded,
                        OSError, urllib.error.URLError) as exc:
                    if not sent:
                        self.send_response(429 if isinstance(exc, BudgetExceeded) else 502)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        # 供应商响应和 URL 可能含秘密；仅返回错误类别。
                        try:
                            self.wfile.write(json.dumps({"error": {
                                "message": "受控代理拒绝或供应商请求失败：" + type(exc).__name__
                            }}).encode())
                        except OSError:
                            pass
                finally:
                    if ticket:
                        broker.budget.finish(ticket, usage, status)

        self.server = UnixHTTP(str(self.path), Handler)
        os.chown(self.path, self.socket_uid, self.socket_gid)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)
