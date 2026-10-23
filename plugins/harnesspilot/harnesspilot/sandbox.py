from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .budget import Budget
from .config import ModelConfig
from .containment import Containment
from .files import inventory, write_tree

UNSAFE = ("--yolo", "--dangerously-bypass-approvals-and-sandbox",
          "--dangerously-bypass-hook-trust")


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    files: dict[str, str]
    seconds: float


def safe_argv(argv: list[str]):
    if any(arg.split("=", 1)[0] in UNSAFE for arg in argv):
        raise ValueError("拒绝高危 Codex 启动参数")


def _scalar(value):
    return json.dumps(value, ensure_ascii=False)


class Sandbox:
    def __init__(self, config: ModelConfig | None = None, budget: Budget | None = None,
                 broker=None):
        if os.name != "posix" or not shutil.which("bwrap"):
            raise RuntimeError("需要 Linux/WSL2 与 bubblewrap；未提供不受限回退执行")
        self.config, self.budget, self.broker = config, budget, broker
        self.binary = Path(os.environ.get(
            "HARNESSPILOT_CODEX_BIN",
            "/opt/harnesspilot/codex-cli/node_modules/.bin/codex"))
        self.codex_root = Path(os.environ.get(
            "HARNESSPILOT_CODEX_ROOT", "/opt/harnesspilot/codex-cli"))
        self.uid = 61234 if os.geteuid() == 0 else os.getuid()
        self.gid = 61234 if os.geteuid() == 0 else os.getgid()

    def _argv(self, work: Path, home: Path, argv: list[str], with_model: bool):
        flags = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                 "--cap-drop", "ALL", "--clearenv", "--dir", "/usr"]
        for name in ("bin", "lib", "lib64", "share", "libexec", "local"):
            path = Path("/usr") / name
            if path.exists():
                flags += ["--ro-bind", str(path), str(path)]
        for name in ("bin", "sbin", "lib", "lib64"):
            path = Path("/") / name
            if path.is_symlink():
                flags += ["--symlink", os.readlink(path), str(path)]
            elif path.is_dir():
                flags += ["--ro-bind", str(path), str(path)]
        flags += ["--proc", "/proc", "--dev", "/dev", "--dir", "/etc",
                  "--dir", "/opt", "--dir", "/opt/hp", "--dir", "/run",
                  "--size", "268435456", "--tmpfs", "/tmp",
                  "--bind", str(work), "/workspace", "--bind", str(home), "/home/runner",
                  "--setenv", "HOME", "/home/runner", "--setenv", "CODEX_HOME", "/home/runner/.codex",
                  "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
                  "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "PYTHONNOUSERSITE", "1",
                  "--chdir", "/workspace"]
        if with_model:
            if not self.binary.is_file() or self.broker is None:
                raise RuntimeError("受控 Codex 二进制或模型代理不可用")
            flags += ["--ro-bind", str(self.codex_root), "/opt/hp/codex-cli",
                      "--symlink", "/opt/hp/codex-cli/node_modules/.bin/codex", "/opt/hp/codex",
                      "--ro-bind", str(Path(__file__).with_name("relay.py")), "/opt/hp/relay.py",
                      "--ro-bind", str(Path(__file__).with_name("native_probe.py")),
                      "/opt/hp/native_probe.py",
                      "--ro-bind", str(self.broker.path), "/run/model.sock"]
            argv = ["/usr/bin/python3", "/opt/hp/relay.py", *argv]
        if os.geteuid() == 0:
            flags = ["/usr/bin/setpriv", f"--reuid={self.uid}", f"--regid={self.gid}",
                     "--clear-groups", "--no-new-privs", *flags]
        return ["/usr/bin/prlimit", "--as=6442450944", "--nproc=512", "--nofile=256",
                "--fsize=67108864", "--cpu=1200", "--", *flags, "--", *argv]

    def run(self, files: dict[str, str], argv: list[str], *, timeout: int = 120,
            with_model: bool = False, export: bool = True) -> Result:
        safe_argv(argv)
        if self.budget:
            self.budget.check()
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="hp-sandbox-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            work, home = root / "work", root / "home"
            work.mkdir(mode=0o700)
            home.mkdir(mode=0o700)
            write_tree(work, files)
            if with_model:
                cfg = self.config
                lines = [f"model = {_scalar(cfg.model)}", f"model_provider = {_scalar(cfg.provider)}",
                         f"model_reasoning_effort = {_scalar(cfg.effort)}",
                         'approval_policy = "never"', 'sandbox_mode = "workspace-write"',
                         "web_search = \"disabled\"", "disable_response_storage = true"]
                lines += [f"{k} = {_scalar(v)}" for k, v in cfg.options.items()]
                lines += ['[sandbox_workspace_write]', 'network_access = false',
                          '[projects."/workspace"]', 'trust_level = "trusted"',
                          f"[model_providers.{_scalar(cfg.provider)}]", 'name = "HarnessPilot controlled proxy"',
                          'base_url = "http://127.0.0.1:19876/v1"', 'wire_api = "responses"',
                          'requires_openai_auth = true', 'supports_websockets = false',
                          'request_max_retries = 0', 'stream_max_retries = 0']
                write_tree(home, {".codex/config.toml": "\n".join(lines) + "\n",
                                  ".codex/auth.json": json.dumps({"OPENAI_API_KEY": self.broker.token})})
                (home / ".codex").chmod(0o700)
                (home / ".codex" / "config.toml").chmod(0o600)
                (home / ".codex" / "auth.json").chmod(0o600)
            for path in [work, home, *work.rglob("*"), *home.rglob("*")]:
                os.chown(path, self.uid, self.gid, follow_symlinks=False)
            if os.geteuid() == 0:
                os.chown(root, self.uid, self.gid)
            command = self._argv(work, home, argv, with_model)
            safe_argv(command)
            with Containment() as group, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                proc = subprocess.Popen(group.command(command), stdin=subprocess.DEVNULL, stdout=stdout,
                                        stderr=stderr, start_new_session=True,
                                        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
                reason = ""
                try:
                    while proc.poll() is None:
                        if time.monotonic() - started > timeout:
                            reason = "沙箱执行超时"
                            break
                        if self.budget:
                            self.budget.check()
                        paths = list(work.rglob("*"))
                        if len(paths) > 6000 or sum(p.lstat().st_size for p in paths) > 268435456:
                            reason = "沙箱输出超出资源限制"
                            break
                        time.sleep(0.1)
                except BaseException:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                stdout.seek(0)
                stderr.seek(0)
                out = stdout.read(8_388_608).decode("utf-8", "replace")
                err = stderr.read(1_048_576).decode("utf-8", "replace")
            result_files = inventory(work, secrets=self.config.secrets() if self.config else []) \
                if export and not reason else {}
            if self.config:
                out, err = self.config.redact(out), self.config.redact(err)
            return Result(124 if reason else proc.returncode, out, err + reason,
                          result_files, round(time.monotonic() - started, 3))

    def codex(self, files: dict[str, str], mission: str, *, timeout: int = 600) -> Result:
        from .native import compile_config, states_from_listing

        tree = {name: text for name, text in files.items()
                if name == "AGENTS.md" or name.startswith(("sub_agents/", ".agents/skills/"))
                or name == ".codex/hooks.json"}
        files = {**files, **compile_config(tree)}
        if ".codex/hooks.json" in tree:
            probe = self.run(files, ["/usr/bin/python3", "/opt/hp/native_probe.py"],
                             timeout=60, with_model=True, export=False)
            if probe.returncode:
                raise RuntimeError("原生 Hooks 发现失败：" + probe.stderr[-1500:])
            states = states_from_listing(json.loads(probe.stdout)["hooks"])
            if not states:
                raise RuntimeError("原生运行时未发现可授权的 Hooks")
            files.update(compile_config(tree, hook_states=states))
        return self.run(files, ["/opt/hp/codex", "exec", "--json", "--ephemeral",
                               "--skip-git-repo-check", "--sandbox", "workspace-write",
                               "-C", "/workspace", mission], timeout=timeout, with_model=True)
