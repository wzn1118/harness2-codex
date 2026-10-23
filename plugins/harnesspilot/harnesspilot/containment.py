"""以 cgroup v2 约束整棵进程树；命名空间与进程组之外的聚合限制。"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
CONTROLLERS = {"cpu", "memory", "pids"}


def delegated_cgroup() -> Path:
    memberships = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    relative = next((line.split(":", 2)[2] for line in memberships
                     if line.startswith("0::")), None)
    if relative is None:
        raise RuntimeError("HarnessPilot requires a delegated cgroup v2 scope")

    current = (CGROUP_ROOT / relative.lstrip("/")).resolve()
    configured = os.environ.get("HARNESSPILOT_CGROUP")
    root = Path(configured).resolve() if configured else current
    if root != CGROUP_ROOT and CGROUP_ROOT not in root.parents:
        raise RuntimeError("HarnessPilot cgroup path must stay inside cgroup v2")
    if current != root and root not in current.parents:
        raise RuntimeError("HarnessPilot cgroup must be the current delegated scope or its parent")

    available = set((root / "cgroup.controllers").read_text(encoding="ascii").split())
    if not CONTROLLERS <= available or not os.access(root / "cgroup.procs", os.W_OK):
        raise RuntimeError(
            "HarnessPilot requires a writable systemd scope delegated with cpu, memory, and pids"
        )

    if not configured:
        manager = root / f"harnesspilot-manager-{os.getpid()}"
        manager.mkdir()
        for pid in (root / "cgroup.procs").read_text(encoding="ascii").split():
            (manager / "cgroup.procs").write_text(pid, encoding="ascii")
        os.environ["HARNESSPILOT_CGROUP"] = str(root)
        enabled = set((root / "cgroup.subtree_control").read_text(encoding="ascii").split())
        missing = CONTROLLERS - enabled
        if missing:
            with (root / "cgroup.subtree_control").open("w", encoding="ascii") as control:
                control.write(" ".join(f"+{name}" for name in sorted(missing)))

    return root


class Containment:
    def __enter__(self):
        root = delegated_cgroup()
        self.path = root / ("run-" + uuid.uuid4().hex)
        self.path.mkdir()
        try:
            for key, value in {"memory.max": "2147483648", "memory.swap.max": "0",
                               "pids.max": "96", "cpu.max": "200000 100000"}.items():
                (self.path / key).write_text(value)
        except BaseException:
            self.path.rmdir()
            raise
        return self

    def command(self, argv):
        return [sys.executable, str(Path(__file__).resolve()), str(self.path), *argv]

    def __exit__(self, *args):
        (self.path / "cgroup.kill").write_text("1")
        for _ in range(100):
            if "populated 0" in (self.path / "cgroup.events").read_text():
                self.path.rmdir()
                return
            time.sleep(0.02)
        raise RuntimeError("cgroup 子进程回收未完成")


if __name__ == "__main__":
    (Path(sys.argv[1]) / "cgroup.procs").write_text(str(os.getpid()))
    os.execv(sys.argv[2], sys.argv[2:])
