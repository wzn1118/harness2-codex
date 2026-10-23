from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .redaction import redact_text

ROOT = Path(__file__).resolve().parents[1]


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def state_root() -> Path:
    root = Path(os.environ.get("HARNESSPILOT_STATE", "~/.local/share/harnesspilot")).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve()


@dataclass(repr=False)
class ModelConfig:
    home: Path
    model: str
    provider: str
    effort: str
    base_url: str = field(repr=False)
    key: str = field(repr=False)
    headers: dict = field(repr=False)
    query: dict = field(repr=False)
    options: dict = field(repr=False)
    fingerprint: str

    @classmethod
    def load(cls, home: Path | None = None) -> ModelConfig:
        home = home or Path(os.environ.get("HARNESSPILOT_CODEX_HOME", "~/.codex")).expanduser()
        config_bytes = (home / "config.toml").read_bytes()
        auth_bytes = (home / "auth.json").read_bytes()
        cfg = tomllib.loads(config_bytes.decode("utf-8-sig"))
        auth = json.loads(auth_bytes)
        auth_mode = auth.get("auth_mode")
        if auth_mode not in (None, "apikey"):
            raise ValueError("当前认证不是 API key 模式；不会把会话认证转换成 API key")
        provider = cfg.get("model_provider", "openai")
        settings = cfg.get("model_providers", {}).get(provider, {})
        if settings.get("wire_api", "responses") != "responses":
            raise ValueError("当前接入需要 Responses 协议；保留原配置并停止模型执行")
        model = cfg.get("model")
        effort = cfg.get("model_reasoning_effort")
        if not isinstance(model, str) or not model.strip() or not isinstance(effort, str):
            raise ValueError("当前 config.toml 必须明确配置模型和推理设置")
        endpoint_override = os.environ.get("HARNESSPILOT_API_BASE_URL", "").strip()
        base = (endpoint_override or settings.get(
            "base_url", "https://api.openai.com/v1")).rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("当前模型地址格式不受支持")
        if parsed.query or parsed.fragment:
            raise ValueError("模型 base_url 不允许查询参数或片段；请使用 query_params")
        key = os.environ.get(settings.get("env_key", ""), "") or auth.get("OPENAI_API_KEY")
        if not isinstance(key, str) or not key:
            raise ValueError("当前 auth.json 未包含可用 API 认证；不会替换登录方式")
        headers = dict(settings.get("http_headers", {}))
        query = settings.get("query_params", {})
        if (not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in query.items())):
            raise ValueError("模型供应商的 header/query 参数格式无效")
        for name, env in settings.get("env_http_headers", {}).items():
            if env not in os.environ:
                raise ValueError("当前供应商所需的环境认证尚未提供")
            headers[name] = os.environ[env]
        options = {name: cfg[name] for name in (
            "model_context_window", "model_auto_compact_token_limit", "model_verbosity",
            "model_reasoning_summary", "model_supports_reasoning_summaries", "service_tier",
        ) if name in cfg}
        fingerprint_input = config_bytes + b"\0" + auth_bytes
        if endpoint_override:
            fingerprint_input += b"\0HARNESSPILOT_API_BASE_URL\0" + base.encode("utf-8")
        return cls(home, model, provider, effort, base, key, headers,
                   query, options,
                   hashlib.sha256(fingerprint_input).hexdigest())

    def public(self) -> dict:
        return {"model": self.model, "provider": self.provider, "reasoning_effort": self.effort,
                "wire_api": "responses", "endpoint_host": urlsplit(self.base_url).hostname,
                "auth_source": "current auth.json",
                "price_usd": None}

    def redact(self, text: str) -> str:
        return redact_text(text, self.secrets())

    def secrets(self) -> list[str]:
        return [self.key, self.base_url, *self.headers.values(), *self.query.values()]


def code_fingerprint() -> str:
    files = {}
    for base in (ROOT / "harnesspilot", ROOT / "upstream/harness2"):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" not in path.parts:
                files[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    binary = Path(os.environ.get(
        "HARNESSPILOT_CODEX_BIN",
        "/opt/harnesspilot/codex-cli/node_modules/.bin/codex"))
    if binary.is_file():
        files["codex_binary"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    codex_package = Path(os.environ.get(
        "HARNESSPILOT_CODEX_ROOT", "/opt/harnesspilot/codex-cli"))
    metadata = codex_package / "node_modules/@openai/codex/package.json"
    if metadata.is_file():
        files["codex_package"] = hashlib.sha256(metadata.read_bytes()).hexdigest()
    return digest(files)
