"""钉住的 Harness² 核心与受控 Codex 执行器之间的适配。"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from .config import ROOT, digest
from .files import editable, inventory, write_tree
from .native import validate
from .suite import judge

sys.path.insert(0, str(ROOT / "upstream"))
from harness2.core.proposer import Proposer, ProposerError, Reply  # noqa: E402
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec  # noqa: E402
from harness2.core.tree import read_tree  # noqa: E402
from harness2.substrate.codex import CodexSubstrate  # noqa: E402


def final_message(text: str) -> str:
    messages = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                messages.append(item.get("text", ""))
        except (ValueError, AttributeError):
            continue
    return "\n".join(messages).strip()


class CodexProposer(Proposer):
    def __init__(self, runner, root: Path, channel: ChannelSpec):
        super().__init__(channel, audit_dir=root, attempts=1)
        self.runner, self.root = runner, root
        self.lock = threading.Lock()

    def call(self, mission, context_dir, label, *, system="", timeout=None,
             max_attempts=None, harvest=None):
        # 上游线程并行保留；单个模型会话串行受理，使预算和恢复的边界确定。
        with self.lock:
            self.runner.budget.check()
            source = inventory(Path(context_dir)) if context_dir else {}
            key = digest({"mission": mission, "system": system, "source": source,
                          "harvest": harvest, "model": self.runner.config.fingerprint})
            folder = self.root / key
            cached = folder / "reply.json"
            if cached.is_file():
                data = json.loads(cached.read_text())
                reply = Reply(label=label, text=data["text"], ok=True, cached=True,
                              workspace=folder / "harvest" if harvest else None,
                              audit=folder / "events.jsonl")
                self._calls.append(reply)
                return reply
            files = {"context/" + name: text for name, text in source.items()}
            files["AGENTS.md"] = (
                "仅按任务操作 context/ 中的材料。候选配置写入 context/harness/。\n"
                "输出结构严格遵循任务要求；用中文写说明。\n")
            result = self.runner.codex(files, system + "\n" + mission,
                                       timeout=min(timeout or 600, 900))
            text = final_message(result.stdout)
            if result.returncode or not text:
                raise ProposerError("Codex 提议会话失败：" + result.stderr[-1200:], attempts=1)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "events.jsonl").write_text(result.stdout, encoding="utf-8")
            workspace = None
            if harvest:
                prefix = harvest.strip("/") + "/"
                tree = {name[len(prefix):]: value for name, value in result.files.items()
                        if name.startswith(prefix)}
                workspace = folder / "harvest"
                write_tree(workspace, tree)
            cached.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
            reply = Reply(label=label, text=text, ok=True, attempts=1, wall_s=result.seconds,
                          workspace=workspace, audit=folder / "events.jsonl", channel="codex")
            self._calls.append(reply)
            return reply


class ProjectAdapter:
    def __init__(self, runner, suite, snapshot: dict, root: Path, *, hooks_approved=False):
        self.runner, self.suite, self.snapshot, self.root = runner, suite, snapshot, root
        self.hooks_approved = hooks_approved
        self.substrate = CodexSubstrate()
        # kind 为上游兼容标识；call() 始终由 CodexProposer 执行。
        self.channel = ChannelSpec("opencode", runner.config.model,
                                   effort=runner.config.effort, timeout=600)
        self.spec = BenchSpec(name="harnesspilot", runs_root=root / "runs",
                              install_mode="codex_root", allow_subagents=True,
                              channels={"proposer": self.channel, "selector": self.channel},
                              prompts=BenchPrompts(work="完成软件实现与修复，交付可运行源码",
                                                   inv_levers="检查输入边界、可复现命令与结果",
                                                   dom_levers="按项目语言和接口约定组织工作"))
        self.base = {name: text for name, text in snapshot.items() if editable(name)}
        self.base.setdefault("AGENTS.md", "按任务实现功能，运行可用检查并如实报告结果。\n")
        validate(self.base, hooks_approved=hooks_approved)
        write_tree(self.base_harness(), self.base)
        for task_id in suite.ids():
            write_tree(self.task_files(task_id), self.source(task_id))
        self.lock = threading.Lock()

    def tasks(self, split="all"):
        return self.suite.ids(split)

    def domain_of(self, task_id):
        return self.suite.tasks[task_id]["kind"]

    def task_prompt(self, task_id):
        return self.suite.tasks[task_id]["prompt"]

    def task_files(self, task_id):
        return self.root / "sources" / task_id

    def source(self, task_id):
        return {**{name: text for name, text in self.snapshot.items() if not editable(name)},
                **self.suite.tasks[task_id].get("sources", {})}

    def base_harness(self):
        return self.root / "base"

    def validate_tree(self, tree):
        validate(tree, hooks_approved=self.hooks_approved)

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        with self.lock:
            self.runner.budget.check()
            tree = read_tree(self.substrate.surface(), harness_dir or self.base_harness())
            self.validate_tree(tree)
            folder = self.root / "executions" / digest(run_id)
            record = folder / "result.json"
            identity = digest({"task": task_id, "tree": tree, "suite": self.suite.sha256})
            if record.is_file():
                data = json.loads(record.read_text())
                if data["identity"] != identity:
                    raise ValueError("恢复缓存与冻结执行参数不一致")
                return folder
            result = self.runner.codex({**self.source(task_id), **tree},
                                       self.task_prompt(task_id), timeout=timeout or 600)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "events.jsonl").write_text(result.stdout, encoding="utf-8")
            (folder / "answer.md").write_text(final_message(result.stdout), encoding="utf-8")
            (folder / "files.json").write_text(json.dumps(result.files, ensure_ascii=False),
                                              encoding="utf-8")
            record.write_text(json.dumps({"identity": identity, "returncode": result.returncode,
                                          "seconds": result.seconds, "error": result.stderr[-2000:]}),
                              encoding="utf-8")
            return folder

    def delivered(self, run_dir):
        try:
            data = json.loads((run_dir / "result.json").read_text())
            return data["returncode"] == 0 and bool(self.deliverable_files(run_dir))
        except (OSError, ValueError):
            return False

    def score(self, run_dir, task_id):
        return judge(self.runner, self.suite.tasks[task_id], self.deliverable_files(run_dir))

    def trajectory(self, run_dir, *, obs_trunc):
        return ("[ACTION] Codex 隔离执行\n[OBSERVATION]\n"
                + (run_dir / "events.jsonl").read_text(encoding="utf-8")[:obs_trunc])

    def deliverable_files(self, run_dir):
        files = json.loads((run_dir / "files.json").read_text(encoding="utf-8"))
        return {n: v for n, v in files.items() if n in {"solution.py", "solution.js", "solution.rs"}}

    def deliverable_text(self, run_dir):
        return "\n\n".join(f"## {n}\n{v}" for n, v in self.deliverable_files(run_dir).items())
