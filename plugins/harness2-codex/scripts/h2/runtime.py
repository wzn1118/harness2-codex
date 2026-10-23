"""Pinned-model Codex runtime with a credential-hiding Responses proxy."""
from __future__ import annotations
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
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

class ProviderFailure(RuntimeError):
    def __init__(self, status, host, request_id=None, retry_after=None, *, circuit_open=False, transport=None):
        self.status = status
        self.host = host
        self.request_id = request_id
        self.retry_after = retry_after
        self.circuit_open = circuit_open
        if circuit_open:
            message = f"Provider circuit open after HTTP {status} from {host}"
        elif transport:
            message = f"Provider transport failure ({transport}) to {host}"
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
        return {"label": label, "status": self.status, "provider_host": self.host,
                "request_id": self.request_id, "retry_after": self.retry_after,
                "circuit_open": self.circuit_open}

def host_config():
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    for path in (home/"requirements.toml",Path("/etc/codex/requirements.toml"),Path("/etc/codex/config.toml"),home/"AGENTS.md",home/"AGENTS.override.md"):
        if path.exists():raise ValueError("Additional host policy/instructions require explicit import; refusing to omit: "+str(path))
    config = tomllib.loads((home / "config.toml").read_text())
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
    provider_name = config.get("model_provider", "openai")
    provider = config.get("model_providers", {}).get(provider_name, {})
    model = config.get("model")
    if not model: raise ValueError("Set an explicit model in the existing Codex config; no silent model substitution")
    auth = json.loads((home / "auth.json").read_text()) if (home / "auth.json").exists() else {}
    key = os.environ.get(provider.get("env_key", "OPENAI_API_KEY")) or auth.get("OPENAI_API_KEY")
    if not key: raise RuntimeError("This release requires an API-key-backed Codex Responses provider; ChatGPT OAuth is not copied or silently substituted")
    base = provider.get("base_url", "https://api.openai.com/v1").rstrip("/")
    if not base.startswith("https://"): raise ValueError("Upstream provider must use HTTPS")
    if provider.get("wire_api", "responses") != "responses": raise ValueError("Only Responses providers are supported")
    sandbox = "read-only" if preset == ":read-only" else config.get("sandbox_mode", "workspace-write")
    features=config.get("features",{})
    envelope={"shell":features.get("shell_tool",True) is not False,"apply_patch":features.get("apply_patch_freeform",True) is not False}
    return {"model": model, "base_url": base, "key": key, "sandbox_mode": sandbox, "approval_policy": config.get("approval_policy", "on-request"), "codex_home":str(home),"tool_envelope":envelope}

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
                except Exception as exc:
                    with owner.lock:
                        owner.infrastructure_errors.setdefault(label,[]).append(str(exc))
                    event(owner.run, "provider_error", label=label, error=str(exc))
                    self.send_error(502, "Provider request failed; see host audit log")
        class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        self.proxy_dir = tempfile.TemporaryDirectory(prefix="h2-proxy-")
        self.server = UnixServer(self.proxy_dir.name + "/api.sock", Proxy)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def _record_provider_failure(self, label, status, request_id=None, retry_after=None, transport=None):
        opens_circuit = status >= 400 or bool(transport)
        failure = ProviderFailure(status, self.provider_host, request_id, retry_after, transport=transport)
        record = failure.as_record(label)
        record["circuit_open"] = opens_circuit
        if transport:
            record["transport"] = transport
        with self.lock:
            if opens_circuit and self.provider_circuit is None:
                self.provider_circuit = {"status": status, "provider_host": self.provider_host,
                                         "request_id": request_id, "retry_after": retry_after}
            self.provider_errors.append(record)
            self.infrastructure_errors.setdefault(label, []).append(str(failure))
        event(self.run, "provider_error", **record, error=str(failure))
        atomic(self.run / "cost-ledger.json", self.ledger())
        return failure

    def _reject_open_circuit(self, label, circuit):
        failure = ProviderFailure(circuit["status"], circuit["provider_host"],
                                  circuit.get("request_id"), circuit.get("retry_after"),
                                  circuit_open=True)
        record = failure.as_record(label)
        record["blocked"] = True
        with self.lock:
            self.provider_errors.append(record)
            self.infrastructure_errors.setdefault(label, []).append(str(failure))
        event(self.run, "provider_circuit_open", **record, error=str(failure))
        atomic(self.run / "cost-ledger.json", self.ledger())
        return failure

    def forward(self, label, body, accept):
        # A single initial request checks provider health; healthy runs retain
        # bounded parallelism, while an outage blocks queued dispatches.
        with self.provider_slots:
            with self.provider_gate:
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
            if not circuit:
                if self.cancel.is_set():
                    raise RuntimeError("Experiment cancelled")
                reservation = self.budget.reserve(label, len(json.dumps(body).encode()) + 8192)
                reserve = reservation["output"]
                self.output_used += reserve
                self.requests += 1
        if circuit:
            raise self._reject_open_circuit(label, circuit)
        body = dict(body)
        body["max_output_tokens"] = reserve
        body["store"] = False
        req = urllib.request.Request(
            self.config["base_url"] + "/responses",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.config["key"],
                     "Content-Type": "application/json", "Accept": accept},
        )
        record = {"at": stamp(), "label": label, "model": self.config["model"],
                  "provider_host": self.provider_host, "reserved_output_tokens": reserve}
        with self.lock:
            self.billing.append(record)
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                payload = response.read(32_000_000)
                content_type = response.headers.get("Content-Type", "application/json")
                record["http_status"] = response.status
                record["provider_request_id"] = _provider_request_id(response.headers)
        except urllib.error.HTTPError as exc:
            record["http_status"] = exc.code
            record["provider_request_id"] = _provider_request_id(exc.headers)
            record["retry_after"] = _retry_after(exc.headers)
            failure = self._record_provider_failure(
                label, exc.code, record["provider_request_id"], record["retry_after"])
            exc.close()
            raise failure from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            record["transport_error"] = type(exc).__name__
            failure = self._record_provider_failure(
                label, 503, transport=type(exc).__name__)
            raise failure from None
        usage = extract_usage(payload)
        record["usage"] = usage
        self.budget.settle(reservation, usage)
        event(self.run, "provider_request", **record)
        atomic(self.run / "cost-ledger.json", self.ledger())
        return payload, content_type

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.proxy_dir.cleanup()

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
            "sandbox_mode = " + json.dumps(sandbox) + "\napproval_policy = " + json.dumps(approval) + "\n"
            "default_permissions = " + json.dumps(":read-only" if sandbox == "read-only" else ":workspace") + "\n"
            "model_reasoning_effort = " + json.dumps(strategy["reasoning_effort"]) + "\nweb_search = \"disabled\"\n"
            "[features]\nshell_tool = " + str(tools["shell"]).lower() + "\napply_patch_freeform = " + str(tools["apply_patch"]).lower() + "\nmulti_agent = false\nhooks = false\napps = false\n"
            "[model_providers.h2proxy]\nname = \"Harness2 credential proxy\"\nwire_api = \"responses\"\nrequires_openai_auth = true\nrequest_max_retries = 0\nstream_max_retries = 0\nbase_url = \"http://127.0.0.1:17280\"\n"
        )
        (home / "config.toml").write_text(config)
        (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY":token}))
        rules=Path(self.config.get("codex_home","/nonexistent"))/"rules"
        if rules.is_dir():shutil.copytree(rules,home/"rules",ignore=shutil.ignore_patterns("*.pyc","__pycache__"))
        mounts=[(self.binary,"/codex"),(self.relay,"/relay.py"),(Path(self.proxy_dir.name),"/broker"),(home/"config.toml","/home/worker/config.toml"),(home/"auth.json","/home/worker/auth.json")]
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
        with self.lock:
            billed=[x for x in self.billing if x.get("label")==label]
        usage={k:sum(int(x.get("usage",{}).get(k,0)) for x in billed if x.get("usage")) for k in ("input_tokens","output_tokens","total_tokens")}
        usage["responses_with_usage"]=sum(bool(x.get("usage")) for x in billed)
        with self.lock:infrastructure_errors=list(self.infrastructure_errors.get(label,[]))
        if result['exit_code'] and not billed and not infrastructure_errors:
            infrastructure_errors.append('Worker launcher failed before any provider request: '+result['stderr'][-1000:])
        record = {"label":label,"at":stamp(),"model":self.config["model"],"sandbox_mode":sandbox,"approval_policy":approval,"wall_s":result["wall_s"],"exit_code":result["exit_code"],"stop_reason":result["stop_reason"],"stderr":result["stderr"][-5000:],"last_message":text,"events":str(audit/(label+".jsonl")),"known_provider_usage":usage,"infrastructure_errors":infrastructure_errors}
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
            invocation_count = len(self.invocations)
            provider_requests = self.requests
            output_reserved = self.output_used
        known = [x["usage"] for x in billing if x.get("usage")]
        totals = {key:sum(int(x.get(key,0)) for x in known) for key in ("input_tokens","output_tokens","total_tokens")}
        totals["cached_input_tokens"] = sum(int(x.get("input_tokens_details",{}).get("cached_tokens",0)) for x in known)
        totals.update({"provider_requests":provider_requests,"usage_complete":len(known)==provider_requests,"output_tokens_reserved":output_reserved,"request_budget":self.max_requests,"output_token_budget":self.output_budget,"cost_usd":None,"cost_status":"unknown: no provider pricing supplied"})
        totals["provider_errors"] = provider_errors
        if self.prices and len(known)==provider_requests:
            totals["cost_usd"] = (totals["input_tokens"]*self.prices["input_per_million"]+totals["output_tokens"]*self.prices["output_per_million"])/1e6
            totals["cost_status"] = "estimate using user-supplied rates; not an invoice"
        totals["invocations"] = invocation_count
        totals['resource_envelopes'] = self.budget.ledger()
        return totals
