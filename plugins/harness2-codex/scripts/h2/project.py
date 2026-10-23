from __future__ import annotations
import json
import os
import re
import shutil
import stat
from pathlib import Path
from .common import digest, relative, target

SKIP = {".git", ".harness2", ".venv", "venv", "node_modules", "__pycache__", ".next", "dist", "build", "target", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SECRET = re.compile(r"(^\.env($|\.)|auth\.json$|credentials|secrets?|\.pem$|\.key$|id_rsa|id_ed25519)", re.I)
TOOLS, STRATEGY = "harness2-tools.json", "harness2-execution.json"
DEFAULT_TOOLS = {"shell": True, "apply_patch": True}
DEFAULT_STRATEGY = {"reasoning_effort": "low", "timeout_sec": 180}

def check_policy_context(root):
    # Removing a project config in a worker copy must not erase restrictions
    # that it (or an ancestor) would have supplied to the real project.
    for base in (root,*root.parents):
        if (base/".codex/config.toml").exists():
            raise ValueError("Project/ancestor Codex config requires explicit policy translation; refusing to omit it: "+str(base/".codex/config.toml"))
        if base!=root and any((base/name).exists() for name in ("AGENTS.md","AGENTS.override.md")):
            raise ValueError("Inherited ancestor instructions require explicit import; refusing to drop them: "+str(base))
    if (root/"AGENTS.override.md").exists():
        raise ValueError("Root AGENTS.override.md is not an editable supported surface")

def reject_worker_links(root):
    # Workers cannot see the host, but host-side upstream readers must never
    # follow a symlink planted by a worker after it exits its namespace.
    for directory,dirs,names in os.walk(root,followlinks=False):
        for name in dirs+names:
            path=Path(directory)/name;info=path.lstat()
            if stat.S_ISLNK(info.st_mode) or (not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1)):
                raise ValueError("Unsafe link/nonregular worker artifact: "+str(path.relative_to(root)))

def scan(root, *, max_files=20000, max_bytes=100_000_000):
    out, size = {}, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP and not (Path(directory) / d).is_symlink() and not SECRET.search(d)]
        for name in names:
            path = Path(directory) / name
            if SECRET.search(name) or path.is_symlink() or not path.is_file(): continue
            rel = path.relative_to(root).as_posix()
            if path.stat().st_nlink != 1: raise ValueError("Hardlinked project file unsupported: " + rel)
            size += path.stat().st_size
            if len(out) >= max_files or size > max_bytes: raise ValueError("Snapshot limit exceeded; select a smaller project root")
            data = path.read_bytes()
            if b"PRIVATE KEY-----" in data or re.search(rb"sk-(?:proj-)?[A-Za-z0-9_-]{40,}", data): raise ValueError("Potential embedded secret: " + rel)
            out[rel] = data
    return out

def snapshot(root, dest):
    files = scan(root)
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, data in files.items():
        output = target(dest, name); output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data); shutil.copymode(root / name, output)
    return {name: digest(data) for name, data in files.items()}

def load_surface(root):
    files, result = scan(root), {}
    for name, data in files.items():
        if name == "AGENTS.md" or re.fullmatch(r"\.agents/skills/[a-z0-9][a-z0-9_-]*/(?:SKILL\.md|scripts/[A-Za-z0-9_.-]+)", name): result[name] = data.decode("utf-8")
    result.setdefault("AGENTS.md", "# Project harness\nFollow the user's task and existing project protections.\n")
    result[TOOLS] = files.get(TOOLS, json.dumps(DEFAULT_TOOLS).encode()).decode()
    result[STRATEGY] = files.get(STRATEGY, json.dumps(DEFAULT_STRATEGY).encode()).decode()
    return result

def validate(surface, original, original_files):
    if set(original) - set(surface): raise ValueError("Existing harness components may not be deleted")
    for name, text in surface.items():
        relative(name)
        allowed = name in {"AGENTS.md", TOOLS, STRATEGY} or bool(re.fullmatch(r"\.agents/skills/[a-z0-9][a-z0-9_-]*/(?:SKILL\.md|scripts/[A-Za-z0-9_.-]+)", name))
        if not allowed: raise ValueError("File outside approved surface: " + name)
        if name in original_files and name not in {TOOLS, STRATEGY}:
            if "/scripts/" in name and text != original.get(name): raise ValueError("Existing helper immutable: " + name)
            if not text.startswith(original[name]): raise ValueError("Existing instructions must remain an exact prefix: " + name)
        if name not in {TOOLS, STRATEGY}:
            added = text[len(original.get(name, "")):] if text.startswith(original.get(name, "")) else text
            if re.search(r"dangerously-bypass|danger-full-access|approval_policy|sandbox_mode|ignore.{0,30}(?:permissions?|approvals?|rules)|disable.{0,30}(?:sandbox|approvals?)", added, re.I): raise ValueError("Permission-relaxing candidate rejected: " + name)
    tools = json.loads(surface[TOOLS])
    if set(tools) != set(DEFAULT_TOOLS) or any(type(v) is not bool for v in tools.values()): raise ValueError("Tools: shell/apply_patch booleans only; arbitrary MCP commands are not editable")
    original_tools=json.loads(original[TOOLS])
    if any(tools[k] and not original_tools[k] for k in DEFAULT_TOOLS):raise ValueError("Tool access cannot expand beyond the original project envelope")
    strategy = json.loads(surface[STRATEGY])
    if set(strategy) != set(DEFAULT_STRATEGY) or strategy["reasoning_effort"] not in {"low", "medium", "high"}: raise ValueError("Strategy: reasoning_effort low/medium/high and timeout_sec only")
    if type(strategy["timeout_sec"]) is not int or not 30 <= strategy["timeout_sec"] <= 600: raise ValueError("timeout_sec must be 30..600")

def materialize(base, dest, surface, *, exclude=()):
    shutil.copytree(base, dest)
    for name in exclude:
        path = target(dest, name)
        if path.exists():
            path.unlink()
    for name in (".codex/config.toml", ".codex/hooks.json", ".mcp.json"):
        path = dest / name
        if path.exists(): path.unlink()
    for name, text in surface.items():
        path = target(dest, name); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)

def tree_hash(root):
    return {name: digest(data) for name, data in scan(root).items()}
