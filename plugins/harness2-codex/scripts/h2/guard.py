"""Linux namespace boundary. No filesystem-copy-as-sandbox fallback."""
from __future__ import annotations
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from .common import atomic, event, stamp

def available():
    bwrap = shutil.which("bwrap")
    if not bwrap: raise RuntimeError("bubblewrap required; install it through your OS package manager. No unsafe fallback.")
    result = subprocess.run([bwrap, "--unshare-all", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev", "--", "/bin/true"], capture_output=True, timeout=10)
    if result.returncode: raise RuntimeError("Namespace sandbox unavailable: " + result.stderr.decode()[-1500:])
    return bwrap

def prefix(work, home=None, *, network=False, read_only=False, mounts=()):
    cmd = [shutil.which("bwrap") or "bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if network: cmd.append("--share-net")
    for system in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(system).exists(): cmd += ["--ro-bind", system, system]
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--dir", "/home", "--dir", "/etc"]
    for system in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl/certs"):
        if Path(system).exists(): cmd += ["--ro-bind", system, system]
    cmd += ["--ro-bind" if read_only else "--bind", str(work), "/work"]
    if home: cmd += ["--bind", str(home), "/home/worker"]
    else: cmd += ["--tmpfs", "/home/worker"]
    for source, dest in mounts: cmd += ["--ro-bind", str(source), dest]
    # CAP_SETFCAP permits mapping uid 0 into Codex's nested user namespace.
    # It grants no host capability. SYS_ADMIN/SYS_PTRACE and every other cap
    # are dropped before starting the worker.
    cmd += ["--chdir", "/work", "--cap-drop", "ALL", "--cap-add", "CAP_SETFCAP", "--clearenv", "--setenv", "HOME", "/home/worker", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8", "--setenv", "LC_ALL", "C.UTF-8"]
    return cmd

def process(cmd, *, prompt=None, timeout=180, cancel=None, stdout_path=None):
    start = time.monotonic()
    # A new process group ensures cancellation and timeout kill every child.
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    stop_reason = None
    def watch():
        nonlocal stop_reason
        while p.poll() is None:
            if (cancel and cancel.is_set()) or time.monotonic() - start > timeout:
                stop_reason = "cancelled" if cancel and cancel.is_set() else "timeout"
                try: os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                break
            time.sleep(.1)
    thread = threading.Thread(target=watch, daemon=True); thread.start()
    output, error = p.communicate(prompt.encode() if prompt is not None else None)
    thread.join(timeout=.5)
    if stdout_path: stdout_path.write_bytes(output)
    return {"exit_code": p.returncode, "stdout": output.decode(errors="replace"), "stderr": error.decode(errors="replace"), "wall_s": round(time.monotonic()-start,3), "stop_reason": stop_reason}

def check_command(work, argv, *, timeout, cancel, output):
    if not isinstance(argv, list) or not argv or any(not isinstance(x,str) or not x for x in argv): raise ValueError("check_argv must be a nonempty argument array; shell strings are not accepted")
    cmd = prefix(work) + ["--"] + argv
    result = process(cmd, timeout=timeout, cancel=cancel)
    atomic(output, result)
    return {"exit_code": result["exit_code"], "wall_s": result["wall_s"], "stop_reason": result["stop_reason"]}
