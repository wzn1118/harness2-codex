from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / "vendor/harness2-upstream"
COMMIT = "1cfe254e8d25b15f37048c3730134686abe60335"
STATE = Path(os.environ.get("HARNESS2_STATE_DIR", "~/.codex/harness2-runs-v1")).expanduser().absolute()
LOCK = threading.RLock()

def stamp():
    return datetime.now(timezone.utc).isoformat()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

def read(path):
    return json.loads(Path(path).read_text())

def atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".atomic-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(canonical(obj) + b"\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def state_key():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.is_symlink(): raise ValueError("State cannot be a symlink")
    path = STATE / ".integrity-key"
    try: fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError: pass
    else:
        with os.fdopen(fd, "wb") as f: f.write(secrets.token_bytes(32))
    if path.is_symlink(): raise ValueError("State key cannot be a symlink")
    return path.read_bytes()

def seal(obj):
    return {"payload": obj, "hmac_sha256": hmac.new(state_key(), canonical(obj), hashlib.sha256).hexdigest()}

def unseal(obj):
    expected = hmac.new(state_key(), canonical(obj["payload"]), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, obj.get("hmac_sha256", "")): raise ValueError("Stored result integrity check failed")
    return obj["payload"]

def runpath(run_id):
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{12}", run_id): raise ValueError("Invalid run id")
    path = STATE / run_id
    if path.is_symlink(): raise ValueError("Run path cannot be a symlink")
    return path

def relative(name):
    p = PurePosixPath(name)
    if not name or "\\" in name or p.is_absolute() or ".." in p.parts or str(p) != name: raise ValueError("Unsafe relative path: " + name)
    return p

def target(root, name):
    if root.is_symlink() or root.resolve()!=root:raise ValueError("Project root was replaced or is noncanonical")
    p, current = relative(name), root
    for part in p.parts:
        current = current / part
        if current.is_symlink(): raise ValueError("Symlink denied: " + name)
    if current.exists() and (not current.is_file() or current.stat().st_nlink != 1): raise ValueError("Nonregular/hardlinked file denied: " + name)
    return current

def filehash(path):
    return digest(path.read_bytes()) if path.exists() else None

def encode_tree(tree):
    return {k: base64.b64encode(v).decode() for k, v in tree.items()}

def decode_tree(tree):
    return {k: base64.b64decode(v, validate=True) for k, v in tree.items()}

def event(run, kind, **data):
    with LOCK:
        with (run / "events.jsonl").open("a", encoding="utf-8") as f: f.write(json.dumps({"at": stamp(), "kind": kind, **data}, ensure_ascii=False) + "\n")

def verify_upstream():
    provenance = read(UPSTREAM / "PROVENANCE.json")
    if provenance["commit"] != COMMIT: raise ValueError("Unexpected upstream revision")
    for entry in provenance["files"]:
        if digest((UPSTREAM / entry["path"]).read_bytes()) != entry["sha256"]: raise ValueError("Upstream source tampered: " + entry["path"])
