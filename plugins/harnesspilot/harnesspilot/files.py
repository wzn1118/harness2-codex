from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath

from .config import digest
from .redaction import redact_text

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ssh",
        ".aws", ".azure", ".cache", "dist", "build", "holdout_private", "suites",
        ".terraform", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SENSITIVE = re.compile(r"(^\.env($|\.)|auth\.json$|credentials|secret|token|\.(pem|key|pfx)$)", re.I)


def relative(name: str) -> str:
    parts = PurePosixPath(name).parts
    if (not name or "\\" in name or ":" in name or "\0" in name
            or name.startswith("/") or any(p in (".", "..") for p in name.split("/"))
            or not parts):
        raise ValueError("拒绝非法相对路径")
    return "/".join(parts)


def editable(name: str) -> bool:
    try:
        name = relative(name)
    except ValueError:
        return False
    return name in {"AGENTS.md", ".codex/hooks.json"} or bool(re.fullmatch(
        r"(?:\.agents/skills/[a-z0-9][a-z0-9_-]*/SKILL\.md|"
        r"\.agents/skills/[a-z0-9][a-z0-9_-]*/scripts/[A-Za-z0-9_.-]+|"
        r"sub_agents/[a-z0-9][a-z0-9_-]*/AGENT\.md|"
        r"scripts/[a-z0-9_]+\.py)", name))


class SecureRoot:
    """逐层打开目录；读取与替换均使用固定目录句柄。"""

    def __init__(self, root: Path):
        if os.name != "posix":
            raise RuntimeError("受控文件操作需要 Linux/WSL2")
        self.path = Path(os.path.abspath(root))
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.path.parts[1:]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.fd)

    @contextlib.contextmanager
    def parent(self, name: str, create: bool = False):
        parts = relative(name).split("/")
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read(self, name: str, limit: int = 1_048_576) -> bytes | None:
        try:
            with self.parent(name) as (parent, leaf):
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                        raise ValueError("拒绝特殊文件、硬链接或过大文件")
                    with os.fdopen(os.dup(fd), "rb") as source:
                        value = source.read(limit + 1)
                    if len(value) > limit:
                        raise ValueError("文件读取超出大小限制")
                    return value
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return None

    def replace(self, name: str, value: bytes | None, mode: int = 0o644):
        with self.parent(name, create=value is not None) as (parent, leaf):
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("目标已变成链接或特殊文件")
            except FileNotFoundError:
                pass
            if value is None:
                try:
                    os.unlink(leaf, dir_fd=parent)
                except FileNotFoundError:
                    pass
            else:
                temporary = f".harnesspilot-{uuid.uuid4().hex}.tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode, dir_fd=parent)
                try:
                    with os.fdopen(fd, "wb") as target:
                        target.write(value)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            os.fsync(parent)


def inventory(root: Path, *, harness_only: bool = False, skip_binary: bool = False,
              secrets: list[str] | None = None) -> dict[str, str]:
    result = {}
    total = 0
    with SecureRoot(root) as secure:
        for directory, dirs, files, fd in os.fwalk(".", dir_fd=secure.fd,
                                                 follow_symlinks=False):
            for name in dirs:
                if name not in SKIP and stat.S_ISLNK(os.stat(
                        name, dir_fd=fd, follow_symlinks=False).st_mode):
                    raise ValueError("项目包含目录符号链接")
            dirs[:] = sorted(d for d in dirs if d not in SKIP)
            # fwalk 的路径由句柄开始；用目录句柄读取，限制链接替换的影响。
            base = directory if isinstance(directory, str) else "."
            for name in sorted(files):
                rel = name if base == "." else f"{base.removeprefix('./')}/{name}"
                if rel.startswith(".codex/") and rel != ".codex/hooks.json":
                    continue
                if SENSITIVE.search(rel) or (harness_only and not editable(rel)):
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("项目包含链接或特殊文件；请在选定范围内处理后重试")
                content = secure.read(rel)
                if content is None:
                    raise ValueError("扫描期间项目发生变化")
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    if skip_binary:
                        continue
                    raise ValueError(f"快照范围包含二进制文件：{rel}；请选择纯文本子项目")
                total += len(content)
                if total > 32 * 1024 * 1024 or len(result) >= 3000:
                    raise ValueError("项目快照超出 3000 文件或 32 MiB 限制；请缩小项目范围")
                result[rel] = redact_text(text, secrets or [])
    return result


def fingerprint(root: Path) -> str:
    return digest(inventory(root))


def write_tree(root: Path, tree: dict[str, str]):
    root.mkdir(parents=True, exist_ok=True)
    with SecureRoot(root) as secure:
        for name, text in sorted(tree.items()):
            if not isinstance(text, str):
                raise ValueError("快照内容必须为文本")
            secure.replace(relative(name), text.encode("utf-8"))


def hashes(tree: dict[str, str]) -> dict[str, str]:
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in tree.items()}
