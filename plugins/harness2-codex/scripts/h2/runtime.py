"""Pinned-model Codex runtime with a credential-hiding Responses proxy."""
from __future__ import annotations
import http.server
import http.client
import hashlib
import json
import os
import re
import secrets
import shutil
import socketserver
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from . import guard
from .budget import Budget, allocation
from .common import ROOT, atomic, event, stamp

MAX_PROVIDER_RETRIES = 2

def _provider_request_id(headers):
    if headers is None:
        return None
    value = headers.get("x-request-id") or headers.get("request-id")
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value.strip()):
        return value.strip()
    return None

def _retry_after(headers):
    if headers is None:
        return None
    value = headers.get("Retry-After")
    return value.strip() if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value.strip()) else None

def _catalog_models(config, home):
    catalog = config.get("model_catalog_json")
    if not isinstance(catalog, str) or not catalog:
        return set()
    path = Path(catalog).expanduser()
    if not path.is_absolute():
        path = home / path
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Configured model catalog cannot be read") from exc
    rows = data.get("models", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("Configured model catalog has an unsupported shape")
    names = set()
    for row in rows:
        if isinstance(row, str):
            names.add(row)
        elif isinstance(row, dict):
            for key in ("slug", "id", "model"):
                value = row.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
    return names

def _key_file_value(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("HARNESS2_PROVIDER_API_KEY_FILE must be an absolute path")
    if path.is_symlink():
        raise ValueError("Provider key file must not be a symbolic link")
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("Configured provider key file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Provider key file must be a regular file")
    if os.name == "posix":
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise ValueError("Provider key file must be owned by the Codex user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Provider key file permissions must be 0600 or stricter")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("Configured provider key file is unavailable") from exc
    if len(raw) > 8192:
        raise ValueError("Provider key file is unexpectedly large")
    try:
        key = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("Provider key file must contain UTF-8 text") from exc
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Provider key file must contain exactly one nonempty key")
    return key

def extract_codex_errors(path, secret=""):
    errors = []
    seen = set()
    request_id_pattern = re.compile(r"\brequest[_ ]id\s*[:=]\s*([A-Za-z0-9._:-]{1,128})", re.I)
    if not Path(path).is_file():
        return errors
    with Path(path).open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") not in {"error", "turn.failed", "response.failed"}:
                continue
            payload = row.get("error")
            payload = payload if isinstance(payload, dict) else row
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                continue
            message = message.strip()[:2000]
            if secret:
                message = message.replace(secret, "[REDACTED]")
            message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", message)
            request_match = request_id_pattern.search(message)
            request_id = request_match.group(1) if request_match else None
            status = payload.get("http_status", payload.get("status_code"))
            if isinstance(status, str) and status.isdigit():
                status = int(status)
            if type(status) is not int or not 100 <= status <= 599:
                status = None
            fingerprint = (request_id, message)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            errors.append({"event_type": row["type"], "message": message,
                           "request_id": request_id, "http_status": status})
    return errors

class ProviderFailure(RuntimeError):
    def __init__(self, status, host, request_id=None, retry_after=None, *, circuit_open=False, transport=None, detail=None, retryable=False, transient_code=None):
        self.status = status
        self.host = host
        self.request_id = request_id
        self.retry_after = retry_after
        self.circuit_open = circuit_open
        self.transport = transport
        self.detail = detail
        self.retryable = retryable
        self.transient_code = transient_code
        if circuit_open:
            message = (f"Provider circuit open after transient {transient_code} (HTTP {status}) from {host}"
                       if transient_code else f"Provider circuit open after HTTP {status} from {host}")
        elif transport:
            message = f"Provider transport failure ({transport}) to {host}"
        elif transient_code:
            message = f"Transient upstream provider error {transient_code} (HTTP {status}) from {host}"
        else:
            message = f"Upstream provider HTTP {status} from {host}"
        if request_id:
            message += f"; request_id={request_id}"
        if retry_after:
            message += f"; retry_after={retry_after}s"
        if not circuit_open and (status >= 400 or transport):
            message += "; circuit opened"
        super().__init__(message)

    def as_record(self, label):
        record={"label": label, "status": self.status, "provider_host": self.host,
                "request_id": self.request_id, "retry_after": self.retry_after,
                "circuit_open": self.circuit_open,"retryable":self.retryable,
                "status_origin": "local_transport_mapping" if self.transport else "provider_http"}
        if self.transient_code:
            record["transient_code"]=self.transient_code
        if self.detail:
            record["detail"]=self.detail
        return record

def host_config(model_provider=None, model=None):
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    for path in (home/"requirements.toml",Path("/etc/codex/requirements.toml"),Path("/etc/codex/config.toml"),home/"AGENTS.md",home/"AGENTS.override.md"):
        if path.exists():raise ValueError("Additional host policy/instructions require explicit import; refusing to omit: "+str(path))
    config = tomllib.loads((home / "config.toml").read_text())
    model_catalog = config.get("model_catalog_json")
    if model_catalog is not None:
        if not isinstance(model_catalog, str) or not model_catalog:
            raise ValueError("Configured model catalog path must be nonempty text")
        catalog_path = Path(model_catalog).expanduser()
        if not catalog_path.is_absolute():
            catalog_path = home / catalog_path
        if not catalog_path.is_file():
            raise ValueError("Configured model catalog cannot be read")
        model_catalog = str(catalog_path.resolve())
    # A separate executor cannot safely infer a selected desktop profile or
    # translate arbitrary named permission profiles. Refuse, never broaden.
    if any(config.get(k) for k in ("profile", "profiles", "permissions")):
        raise ValueError("Named configuration/permission profiles require an explicit adapter; refusing to guess their effective permissions")
    preset = config.get("default_permissions")
    if preset not in (None, ":workspace", ":read-only"):
        raise ValueError("Unsupported permission preset; only :workspace and :read-only can be preserved")
    if config.get("sandbox_mode", "workspace-write") not in ("read-only", "workspace-write", "danger-full-access"):
        raise ValueError("Unknown sandbox mode; refusing permission translation")
    if any(config.get(k) for k in ("sandbox_workspace_write", "sandbox_read_only")):
        raise ValueError("Custom sandbox filesystem policies require an explicit adapter; refusing to weaken them")
    if config.get("approval_policy", "on-request") not in ("on-request", "never"):
        raise ValueError("Unsupported approval policy; refusing to substitute a different policy")
    provider_name = model_provider or config.get("model_provider", "openai")
    if not isinstance(provider_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", provider_name):
        raise ValueError("model_provider must name a configured Codex provider")
    provider = config.get("model_providers", {}).get(provider_name)
    if provider is None and provider_name == "openai":
        provider = {}
    if not isinstance(provider, dict):
        raise ValueError("Requested model_provider is not configured in the host Codex config")
    configured_model = config.get("model")
    selected_model = model or configured_model
    if not isinstance(selected_model, str) or not selected_model:
        raise ValueError("Set an explicit model in the existing Codex config; no silent model substitution")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}", selected_model):
        raise ValueError("model must be a valid model-catalog identifier")
    if model and model != configured_model and model not in _catalog_models(config, home):
        raise ValueError("Requested model is not present in the configured Codex model catalog")
    auth = json.loads((home / "auth.json").read_text()) if (home / "auth.json").exists() else {}
    if not isinstance(auth, dict):
        raise ValueError("Codex auth.json must contain a JSON object")
    env_key = provider.get("env_key", "OPENAI_API_KEY")
    if not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_key):
        raise ValueError("Configured provider env_key is invalid")
    key = os.environ.get(env_key) or auth.get(env_key) or os.environ.get("HARNESS2_PROVIDER_API_KEY")
    if not key and os.environ.get("HARNESS2_PROVIDER_API_KEY_FILE"):
        key = _key_file_value(os.environ["HARNESS2_PROVIDER_API_KEY_FILE"])
    if not isinstance(key, str) or not key.strip(): raise RuntimeError("This release requires an API-key-backed Codex Responses provider; ChatGPT OAuth is not copied or silently substituted")
    base = provider.get("base_url", "https://api.openai.com/v1")
    if not isinstance(base, str): raise ValueError("Configured provider base_url must be text")
    base = base.rstrip("/")
    parsed_base = urllib.parse.urlsplit(base)
    if parsed_base.scheme != "https" or not parsed_base.hostname or parsed_base.username or parsed_base.password:
        raise ValueError("Upstream provider must use an HTTPS URL without embedded credentials")
    if provider.get("wire_api", "responses") != "responses": raise ValueError("Only Responses providers are supported")
    sandbox = "read-only" if preset == ":read-only" else config.get("sandbox_mode", "workspace-write")
    features=config.get("features",{})
    envelope={"shell":features.get("shell_tool",True) is not False,"apply_patch":features.get("apply_patch_freeform",True) is not False}
    return {"model": selected_model, "model_provider": provider_name,
            "provider_host": parsed_base.hostname, "base_url": base, "key": key,
            "sandbox_mode": sandbox, "approval_policy": config.get("approval_policy", "on-request"),
            "codex_home":str(home),"model_catalog_json":model_catalog,"tool_envelope":envelope}

def binary_path():
    executable = Path(shutil.which("codex") or "/not-found").resolve()
    if executable.name == "codex" and executable.is_file(): return executable
    candidates = list(executable.parents[1].glob("node_modules/@openai/codex-*/vendor/*/bin/codex")) if executable.exists() else []
    if not candidates: raise RuntimeError("Native Codex executable not found; install the official Codex CLI")
    return candidates[0]

def extract_usage(payload):
    rows = []
    try:
        value = json.loads(payload)
        if isinstance(value,dict): rows.append(value)
    except (ValueError, UnicodeDecodeError):
        for line in payload.decode(errors="replace").splitlines():
            if line.startswith("data: "):
                try: rows.append(json.loads(line[6:]))
                except ValueError: pass
    candidates = []
    for row in rows:
        response = row.get("response", row) if isinstance(row,dict) else {}
        usage = response.get("usage") if isinstance(response,dict) else None
        if isinstance(usage,dict): candidates.append(usage)
    return candidates[-1] if candidates else None

def _provider_error_detail(error, key):
    try:
        payload=json.loads(error.read(8192).decode("utf-8",errors="replace"))
    except (OSError,ValueError,AttributeError):
        return None,None
    detail=payload.get("error") if isinstance(payload,dict) else None
    if not isinstance(detail,dict):
        return None,None
    text=" ".join(str(detail[name]) for name in ("type","code","message","param")
                  if detail.get(name) is not None)
    if key:
        text=text.replace(key,"[REDACTED]")
    text=re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text=re.sub(r"\b(?:sk|ak|rk)-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", text)
    text=" ".join(text.split())
    code=detail.get("code")
    if not isinstance(code,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}",code):
        code=None
    return text[:500] or None,code

class Runtime:
    def __init__(self, run, cancel, config, max_requests=160, max_output_tokens=655360, prices=None, budget_request=None, execution_timeout_sec=None):
        self.run, self.cancel, self.config = run, cancel, config
        self.max_requests, self.output_budget = max_requests, max_output_tokens
        self.prices = prices
        limits, plan = allocation(budget_request or {'max_requests':max_requests,'max_output_tokens':max_output_tokens})
        self.budget = Budget(limits, plan)
        self.execution_timeout_sec = execution_timeout_sec
        self.requests, self.output_used = 0, 0
        self.invocations, self.billing = [], []
        self.codex_retries = []
        self.provider_circuit_resets = []
        self.provider_retries = []
        self.worker_errors = []
        self.infrastructure_errors = {}
        self.provider_errors = []
        self.provider_host = urllib.parse.urlsplit(config["base_url"]).hostname or "unknown"
        self.provider_circuit = None
        self.provider_ready = False
        self.provider_gate = threading.Lock()
        self.provider_slots = threading.BoundedSemaphore(8)
        self.lock = threading.RLock()
        self.tokens = {}
        self.binary = binary_path()
        assets=self.run/'runtime-assets';assets.mkdir(exist_ok=True,mode=0o700)
        self.relay=assets/'relay.py'
        shutil.copy2(ROOT/'scripts/h2/relay.py',self.relay)
        self.model_catalog_asset = None
        self.model_catalog_sha256 = None
        catalog_path = config.get("model_catalog_json")
        if catalog_path:
            source = Path(catalog_path)
            if source.is_symlink() or not source.is_file():
                raise ValueError("Configured model catalog is unavailable or unsafe")
            self.model_catalog_asset = assets/'model-catalog.json'
            shutil.copyfile(source, self.model_catalog_asset)
            os.chmod(self.model_catalog_asset, 0o400)
            self.model_catalog_sha256 = hashlib.sha256(self.model_catalog_asset.read_bytes()).hexdigest()
        owner = self
        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def provider_error(self, failure):
                payload = json.dumps({"error": {"message": str(failure), "type": "upstream_error"}}).encode()
                self.send_response(failure.status)
                self.send_header("Content-Type", "application/json")
                if failure.retry_after:
                    self.send_header("Retry-After", failure.retry_after)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def do_POST(self):
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                with owner.lock:
                    label = owner.tokens.get(token)
                if not label or self.path not in {"/responses", "/v1/responses"}:
                    self.send_error(403); return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4_000_000:
                    self.send_error(413); return
                try:
                    body = json.loads(self.rfile.read(length))
                    if body.get("model") != owner.config["model"]: raise ValueError("Model mismatch")
                    if body.get("previous_response_id"): raise ValueError("Cross-session response reuse denied")
                    payload, content_type = owner.forward(label, body, self.headers.get("Accept", "application/json"))
                    self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
                except ProviderFailure as exc:
                    self.provider_error(exc)
                except ValueError as exc:
                    status = 429 if "resource budget exhausted" in str(exc).casefold() else 400
                    with owner.lock:
                        owner.infrastructure_errors.setdefault(label,[]).append(str(exc))
                    event(owner.run,"broker_request_rejected",label=label,local_http_status=status,error=str(exc))
                    self.send_error(status,"Harness2 local request rejected; see host audit log")
                except Exception as exc:
                    with owner.lock:
                        owner.infrastructure_errors.setdefault(label,[]).append(str(exc))
                    event(owner.run, "broker_error", label=label, error=str(exc))
                    self.send_error(500, "Harness2 local broker failure; see host audit log")
        class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        self.proxy_dir = tempfile.TemporaryDirectory(prefix="h2-proxy-")
        self.server = UnixServer(self.proxy_dir.name + "/api.sock", Proxy)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def _record_provider_failure(self, label, status, request_id=None, retry_after=None, transport=None, detail=None, transient_code=None):
        opens_circuit = status >= 400 or bool(transport)
        retryable=status in (502,503,504) or transient_code=="upstream_unavailable"
        failure = ProviderFailure(status, self.provider_host, request_id, retry_after, transport=transport,
                                  detail=detail,retryable=retryable,transient_code=transient_code)
        record = failure.as_record(label)
        record["circuit_open"] = opens_circuit
        if transport:
            record["transport"] = transport
        with self.lock:
            if opens_circuit and self.provider_circuit is None:
                self.provider_circuit = {"status": status, "provider_host": self.provider_host,
                                         "request_id": request_id, "retry_after": retry_after,
                                         "retryable":retryable,"transient_code":transient_code,
                                         "origin_label":label}
            self.provider_errors.append(record)
            self.infrastructure_errors.setdefault(label, []).append(str(failure))
        event(self.run, "provider_error", **record, error=str(failure))
        atomic(self.run / "cost-ledger.json", self.ledger())
        return failure

    def _reject_open_circuit(self, label, circuit):
        failure = ProviderFailure(circuit["status"], circuit["provider_host"],
                                  circuit.get("request_id"), circuit.get("retry_after"),
                                  circuit_open=True,retryable=circuit.get("retryable",False),
                                  transient_code=circuit.get("transient_code"))
        record = failure.as_record(label)
        record["blocked"] = True
        with self.lock:
            self.provider_errors.append(record)
            self.infrastructure_errors.setdefault(label, []).append(str(failure))
        event(self.run, "provider_circuit_open", **record, error=str(failure))
        atomic(self.run / "cost-ledger.json", self.ledger())
        return failure

    def reset_transient_provider_circuit(self, label, reason):
        with self.lock:
            circuit=self.provider_circuit
            if not circuit or not (circuit.get("status") in (502,503,504) or circuit.get("retryable")):
                return False
            self.provider_circuit=None
            record={"label":label,"status":circuit["status"],
                    "provider_host":circuit["provider_host"],
                    "request_id":circuit.get("request_id"),"reason":reason[:1000],
                    "transient_code":circuit.get("transient_code")}
            self.provider_circuit_resets.append(record)
        event(self.run,"provider_circuit_reset",**record)
        atomic(self.run/"cost-ledger.json",self.ledger())
        return True

    def _record_provider_retry(self, label, status, request_id, retry_after, *, attempt, transport=None, detail=None, transient_code=None):
        error = {"label":label,"status":status,"provider_host":self.provider_host,
                 "request_id":request_id,"retry_after":retry_after,
                 "circuit_open":False,
                 "status_origin":"local_transport_mapping" if transport else "provider_http",
                 "retryable":True,"attempt":attempt,"recovered":False}
        if transport:
            error["transport"] = transport
        if detail:
            error["detail"] = detail
        if transient_code:
            error["transient_code"] = transient_code
        retry = {"label":label,"status":status,"request_id":request_id,"attempt":attempt,
                 "retry_after":retry_after,"recovered":False}
        if transport:
            retry["transport"] = transport
        if transient_code:
            retry["transient_code"] = transient_code
        with self.lock:
            self.provider_errors.append(error)
            self.provider_retries.append(retry)
        event(self.run,"provider_retry",**retry)
        atomic(self.run/"cost-ledger.json",self.ledger())
        return error,retry

    def forward(self, label, body, accept):
        # New independent sessions may probe a transient outage again. The
        # failed session stays bounded; auth and quota circuits stay closed.
        with self.provider_slots:
            with self.provider_gate:
                with self.lock:
                    circuit = dict(self.provider_circuit) if self.provider_circuit else None
                if (circuit and circuit.get("retryable") and circuit.get("origin_label")
                        and circuit["origin_label"] != label):
                    self.reset_transient_provider_circuit(
                        label,"New isolated session after transient failure: " + circuit["origin_label"])
                    with self.lock:
                        circuit = dict(self.provider_circuit) if self.provider_circuit else None
                if circuit:
                    raise self._reject_open_circuit(label, circuit)
                if not self.provider_ready:
                    result = self._dispatch_provider(label, body, accept)
                    self.provider_ready = True
                    return result
            return self._dispatch_provider(label, body, accept)

    def _dispatch_provider(self, label, body, accept):
        with self.lock:
            circuit = dict(self.provider_circuit) if self.provider_circuit else None
        if circuit:
            raise self._reject_open_circuit(label, circuit)
        retry_audits=[]
        for attempt in range(MAX_PROVIDER_RETRIES+1):
            with self.lock:
                circuit = dict(self.provider_circuit) if self.provider_circuit else None
                if not circuit:
                    if self.cancel.is_set():
                        raise RuntimeError("Experiment cancelled")
                    reservation = self.budget.reserve(label, len(json.dumps(body).encode()) + 8192)
                    reserve = reservation["output"]
                    self.output_used += reserve
                    self.requests += 1
            if circuit:
                raise self._reject_open_circuit(label, circuit)
            attempt_body = dict(body)
            attempt_body["max_output_tokens"] = reserve
            attempt_body["store"] = False
            req = urllib.request.Request(
                self.config["base_url"] + "/responses",
                data=json.dumps(attempt_body).encode(),
                headers={"Authorization": "Bearer " + self.config["key"],
                         "Content-Type": "application/json", "Accept": accept},
            )
            record = {"at": stamp(), "label":label, "model":self.config["model"],
                      "model_provider":self.config.get("model_provider"),
                      "provider_host":self.provider_host, "reserved_output_tokens":reserve,
                      "reserved_total_tokens":reservation["tokens"],
                      "request_bytes":len(req.data),
                      "attempt":attempt+1}
            with self.lock:
                self.billing.append(record)
            try:
                with urllib.request.urlopen(req, timeout=180) as response:
                    record["http_status"] = response.status
                    record["provider_request_id"] = _provider_request_id(response.headers)
                    content_type = response.headers.get("Content-Type", "application/json")
                    payload = response.read(32_000_000)
                    record["response_read_complete"] = True
            except urllib.error.HTTPError as exc:
                record["http_status"] = exc.code
                record["provider_request_id"] = _provider_request_id(exc.headers)
                record["retry_after"] = _retry_after(exc.headers)
                detail,provider_code=_provider_error_detail(exc,self.config["key"])
                if detail:
                    record["provider_error_detail"] = detail
                transient_code=provider_code if provider_code=="upstream_unavailable" else None
                retryable=exc.code in (502,503,504) or transient_code is not None
                if attempt < MAX_PROVIDER_RETRIES and retryable:
                    error_record,retry_audit=self._record_provider_retry(
                        label,exc.code,record["provider_request_id"],record["retry_after"],
                        attempt=attempt+1,detail=detail,transient_code=transient_code)
                    retry_audits.append((error_record,retry_audit))
                    event(self.run,"provider_request",**record)
                    exc.close()
                    delay=int(record["retry_after"] or str(2**attempt))
                    if self.cancel.wait(min(delay,30)):
                        raise RuntimeError("Experiment cancelled during provider retry")
                    continue
                failure = self._record_provider_failure(
                    label, exc.code, record["provider_request_id"], record["retry_after"],
                    detail=detail,transient_code=transient_code)
                exc.close()
                raise failure from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                record["transport_error"] = type(exc).__name__
                if attempt == 0 and isinstance(exc, http.client.IncompleteRead):
                    error_record,retry_audit=self._record_provider_retry(
                        label,None,record.get("provider_request_id"),None,attempt=attempt+1,
                        transport=type(exc).__name__)
                    retry_audits.append((error_record,retry_audit))
                    event(self.run,"provider_request",**record)
                    if self.cancel.wait(1):
                        raise RuntimeError("Experiment cancelled during provider stream retry")
                    continue
                failure = self._record_provider_failure(
                    label, 503, transport=type(exc).__name__)
                raise failure from None
            if retry_audits:
                with self.lock:
                    for error_record,retry_audit in retry_audits:
                        retry_audit["recovered"] = True
                        error_record["recovered"] = True
                for _,retry_audit in retry_audits:
                    event(self.run,"provider_retry_recovered",**dict(retry_audit))
            usage = extract_usage(payload)
            record["usage"] = usage
            try:
                adjustment = self.budget.settle(reservation, usage)
                if adjustment:
                    record["usage_adjustment"] = adjustment
            except ValueError as exc:
                record["usage_accounting_error"] = str(exc)
                raise
            finally:
                event(self.run, "provider_request", **record)
                atomic(self.run / "cost-ledger.json", self.ledger())
            return payload, content_type
        raise RuntimeError("Provider retry loop ended unexpectedly")

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.proxy_dir.cleanup()

    def record_codex_retry(self, failed_label, retry_label, reason, *, recovered, resolved_label=None):
        record = {"failed_label":failed_label,"retry_label":retry_label,
                  "reason":reason[:1000],"recovered":bool(recovered),
                  "resolved_label":resolved_label if recovered else None}
        with self.lock:
            self.codex_retries.append(record)
            if recovered:
                for invocation in self.invocations:
                    if invocation.get("label") == failed_label:
                        invocation["recovered_by"] = resolved_label or retry_label
                        atomic(self.run/'invocations'/(failed_label+'.json'), invocation)
                        break
        event(self.run,"codex_invocation_retry",**record)
        return record

    def call(self, work, mission, label, *, strategy=None, tools=None, readonly=False):
        if self.cancel.is_set(): raise RuntimeError("Experiment cancelled")
        strategy = strategy or {"reasoning_effort":"low","timeout_sec":180}
        tools = tools or {"shell":True,"apply_patch":True}
        tools={k:bool(v) and self.config.get("tool_envelope",{}).get(k,True) for k,v in tools.items()}
        token = secrets.token_urlsafe(32)
        home = self.run / "worker-homes" / label
        home.mkdir(parents=True,exist_ok=False,mode=0o700)
        with self.lock: self.tokens[token] = label
        policy = self.config["sandbox_mode"]
        # Never elevate a read-only policy; cap all broader policies at
        # workspace-write inside a second OS-level namespace boundary.
        sandbox = "read-only" if readonly or policy == "read-only" else "workspace-write"
        approval = self.config["approval_policy"]
        config = (
            "model = " + json.dumps(self.config["model"]) + "\nmodel_provider = \"h2proxy\"\n"
            + ("model_catalog_json = \"/home/worker/model-catalog.json\"\n" if self.model_catalog_asset else "")
            + "sandbox_mode = " + json.dumps(sandbox) + "\napproval_policy = " + json.dumps(approval) + "\n"
            + "default_permissions = " + json.dumps(":read-only" if sandbox == "read-only" else ":workspace") + "\n"
            + "model_reasoning_effort = " + json.dumps(strategy["reasoning_effort"]) + "\nweb_search = \"disabled\"\n"
            + "[features]\nshell_tool = " + str(tools["shell"]).lower() + "\napply_patch_freeform = " + str(tools["apply_patch"]).lower() + "\nmulti_agent = false\nhooks = false\napps = false\n"
            + "[model_providers.h2proxy]\nname = \"Harness2 credential proxy\"\nwire_api = \"responses\"\nrequires_openai_auth = true\nrequest_max_retries = 0\nstream_max_retries = 0\nbase_url = \"http://127.0.0.1:17280\"\n"
        )
        (home / "config.toml").write_text(config)
        (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY":token}))
        rules=Path(self.config.get("codex_home","/nonexistent"))/"rules"
        if rules.is_dir():shutil.copytree(rules,home/"rules",ignore=shutil.ignore_patterns("*.pyc","__pycache__"))
        mounts=[(self.binary,"/codex"),(self.relay,"/relay.py"),(Path(self.proxy_dir.name),"/broker"),(home/"config.toml","/home/worker/config.toml"),(home/"auth.json","/home/worker/auth.json")]
        if self.model_catalog_asset:
            (home/"model-catalog.json").touch(mode=0o400)
            mounts.append((self.model_catalog_asset,"/home/worker/model-catalog.json"))
        if (home/"rules").is_dir():mounts.append((home/"rules","/home/worker/rules"))
        if (work/".codex/rules").is_dir():mounts.append((work/".codex/rules","/work/.codex/rules"))
        cmd = guard.prefix(work, home, read_only=sandbox=="read-only", mounts=mounts)
        cmd += ["--setenv","CODEX_HOME","/home/worker","--", "/usr/bin/python3","/relay.py","/codex","exec","--json","--ephemeral","--skip-git-repo-check","--cd","/work","--output-last-message","/home/worker/last.txt","-"]
        audit = self.run / "invocations";audit.mkdir(exist_ok=True)
        event(self.run,"invocation_started",label=label)
        result = guard.process(cmd,prompt=mission,timeout=self.execution_timeout_sec or strategy["timeout_sec"],cancel=self.cancel,stdout_path=audit/(label+".jsonl"))
        with self.lock: self.tokens.pop(token,None)
        last = home / "last.txt"
        text = last.read_text() if last.exists() else ""
        invocation_events = audit/(label+".jsonl")
        codex_errors = extract_codex_errors(invocation_events, self.config["key"])
        with self.lock:
            billed=[x for x in self.billing if x.get("label")==label]
            self.worker_errors.extend({"label":label, **error} for error in codex_errors)
        usage={k:sum(int(x.get("usage",{}).get(k,0)) for x in billed if x.get("usage")) for k in ("input_tokens","output_tokens","total_tokens")}
        usage["responses_with_usage"]=sum(bool(x.get("usage")) for x in billed)
        usage["provider_requests"]=len(billed)
        usage["responses_missing_usage"]=len(billed)-usage["responses_with_usage"]
        with self.lock:infrastructure_errors=list(self.infrastructure_errors.get(label,[]))
        infrastructure_errors.extend(
            "Codex worker reported " + error["event_type"] + ": " + error["message"]
            + ("; request_id=" + error["request_id"] if error.get("request_id") else "")
            for error in codex_errors
        )
        if result['exit_code'] and not billed and not infrastructure_errors:
            infrastructure_errors.append('Worker launcher failed before any provider request: '+result['stderr'][-1000:])
        elif result["exit_code"] and not usage["responses_with_usage"] and not infrastructure_errors:
            infrastructure_errors.append(
                f"Codex worker exited {result['exit_code']} after {len(billed)} provider request(s); usage unavailable"
            )
        record = {"label":label,"at":stamp(),"model":self.config["model"],"model_provider":self.config.get("model_provider"),"provider_host":self.provider_host,"sandbox_mode":sandbox,"approval_policy":approval,"wall_s":result["wall_s"],"exit_code":result["exit_code"],"stop_reason":result["stop_reason"],"stderr":result["stderr"][-5000:],"last_message":text,"events":str(invocation_events),"known_provider_usage":usage,"codex_errors":codex_errors,"infrastructure_errors":infrastructure_errors}
        atomic(audit/(label+".json"),record)
        with self.lock: self.invocations.append(record)
        event(self.run,"invocation_finished",label=label,exit_code=result["exit_code"])
        atomic(self.run/"cost-ledger.json",self.ledger())
        # Dummy per-call proxy token is revoked, not persisted.
        (home / "auth.json").unlink(missing_ok=True)
        return record

    def ledger(self):
        with self.lock:
            billing = list(self.billing)
            provider_errors = [dict(x) for x in self.provider_errors]
            provider_retries = [dict(item) for item in self.provider_retries]
            invocation_count = len(self.invocations)
            provider_requests = self.requests
            output_reserved = self.output_used
            worker_errors = [dict(x) for x in self.worker_errors]
            codex_retries = [dict(item) for item in self.codex_retries]
            provider_circuit_resets = [dict(item) for item in self.provider_circuit_resets]
        known = [x["usage"] for x in billing if x.get("usage")]
        totals = {key:sum(int(x.get(key,0)) for x in known) for key in ("input_tokens","output_tokens","total_tokens")}
        totals["cached_input_tokens"] = sum(int(x.get("input_tokens_details",{}).get("cached_tokens",0)) for x in known)
        totals.update({"provider_requests":provider_requests,"usage_complete":len(known)==provider_requests,"output_tokens_reserved":output_reserved,"request_budget":self.max_requests,"output_token_budget":self.output_budget,"cost_usd":None,"cost_status":"unknown: no provider pricing supplied"})
        totals["responses_with_usage"] = len(known)
        totals["provider_responses_missing_usage"] = max(0, provider_requests-len(known))
        totals["provider_errors"] = provider_errors
        totals["provider_retries"] = provider_retries
        totals["codex_worker_errors"] = worker_errors
        if self.prices and len(known)==provider_requests:
            totals["cost_usd"] = (totals["input_tokens"]*self.prices["input_per_million"]+totals["output_tokens"]*self.prices["output_per_million"])/1e6
            totals["cost_status"] = "estimate using user-supplied rates; not an invoice"
        totals["invocations"] = invocation_count
        totals["codex_retries"] = codex_retries
        totals["provider_circuit_resets"] = provider_circuit_resets
        totals["model_catalog"] = ({"sha256":self.model_catalog_sha256,"mounted_read_only":True}
                                   if self.model_catalog_asset else {"configured":False})
        totals['resource_envelopes'] = self.budget.ledger()
        return totals
