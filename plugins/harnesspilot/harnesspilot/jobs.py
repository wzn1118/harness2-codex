from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import threading
import time
import tempfile
import traceback
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

from .bridge import CodexProposer, ProjectAdapter
from .broker import Broker
from .budget import Budget, Limits
from .config import ModelConfig, code_fingerprint, digest
from .files import SecureRoot, editable, hashes, inventory
from .redaction import redact_text
from .sandbox import Sandbox
from .statistics import paired
from .store import Store
from .suite import Suite


def project_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("需要明确指定项目绝对路径")
    value = value.strip()
    if len(value) >= 3 and value[1:3] == ":\\":
        value = f"/mnt/{value[0].lower()}/{value[3:].replace(chr(92), '/')}"
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("项目路径必须为绝对路径")
    return path


class HarnessPilot:
    def __init__(self, store: Store | None = None, suite: Suite | None = None):
        self.store = store or Store()
        self.suite = suite or Suite()
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._budgets: dict[str, Budget] = {}

    def scan(self, path: str) -> dict:
        root = project_path(path)
        config = ModelConfig.load()
        files = inventory(root, skip_binary=True, secrets=config.secrets())
        if not files:
            raise ValueError("项目中未发现可扫描的文本文件")
        ident = uuid.uuid4().hex
        tree_hashes = hashes(files)
        artifact = self.store.artifact("snapshots", ident)
        self.store.export("tree.json", files, artifact)
        harness = {name: text for name, text in files.items() if editable(name)}
        record = {"project_root": str(root), "sha256": digest(tree_hashes),
                  "harness_sha256": digest(hashes(harness)),
                  "file_count": len(files), "editable_count": sum(
                      name == "AGENTS.md" or name.startswith((".agents/skills/", "sub_agents/",
                                                               "scripts/", ".codex/hooks.json"))
                      for name in files), "created": time.time(),
                  "snapshot_path": str(artifact / "tree.json")}
        self.store.create("snapshot", record, ident=ident)
        return {"snapshot_id": ident, "sha256": record["sha256"],
                "file_count": record["file_count"], "editable_count": record["editable_count"],
                "model": config.public(), "suite": self.suite.public(),
                "limits": {"snapshot_bytes": 32 * 1024 * 1024, "snapshot_files": 3000}}

    def tasks(self, split: str = "all") -> dict:
        if split not in {"all", "train", "validation", "sealed"}:
            raise ValueError("split 需要为 all/train/validation/sealed")
        return {"sha256": self.suite.sha256, "split": split,
                "tasks": [self.suite.public_task(ident) for ident in self.suite.ids(split)]}

    def estimate(self, kind: str, snapshot_id: str, *, baseline_job: str = "",
                 candidate_id: str = "", k: int = 1, limits: dict | None = None,
                 allow_hooks: bool = False) -> dict:
        snapshot = self.store.get("snapshot", snapshot_id)
        cfg = ModelConfig.load()
        parsed_limits = Limits(**(limits or {}))
        if type(allow_hooks) is not bool:
            raise ValueError("allow_hooks 必须为布尔值")
        if kind == "baseline":
            task_ids = self.suite.ids("train") + self.suite.ids("validation")
            requests = len(task_ids)
            inputs = {"task_ids": task_ids}
        elif kind == "optimize":
            if type(k) is not int or not 1 <= k <= 4:
                raise ValueError("k 需要在 1 至 4 之间")
            baseline = self.store.get("job", baseline_job)
            if (baseline.get("kind") != "baseline" or baseline.get("status") != "complete"
                    or baseline.get("snapshot_id") != snapshot_id):
                raise ValueError("需要同一项目快照下已完成的基线作业")
            train_ids = self.suite.ids("train")
            validation_ids = self.suite.ids("validation")
            if not set(train_ids + validation_ids).issubset(baseline.get("task_ids", [])):
                raise ValueError("基线没有覆盖全部 train 与 validation 任务")
            if baseline.get("model_fingerprint") != cfg.fingerprint:
                raise ValueError("基线模型配置已变化；请用当前配置重建基线")
            requests = len(train_ids) * (13 * k + 4 + (2 if k > 1 else 0))
            requests += len(validation_ids) * 4
            inputs = {"baseline_job": baseline_job, "train_ids": train_ids,
                      "validation_ids": validation_ids, "k": k}
        elif kind == "sealed":
            job = self.store.get("job", baseline_job)
            candidate = self._candidate(job, candidate_id)
            if (job.get("kind") != "optimize" or job.get("status") != "complete"
                    or candidate_id != job.get("selected_candidate")):
                raise ValueError("sealed 复评只接受已冻结的 validation 最优候选")
            requests = 2 * len(self.suite.ids("sealed"))
            inputs = {"optimization_job": baseline_job, "candidate_id": candidate_id,
                      "sealed_ids": self.suite.ids("sealed")}
        else:
            raise ValueError("kind 需要为 baseline/optimize/sealed")
        estimated_tokens = requests * (parsed_limits.output_tokens + 32768)
        token_fit = estimated_tokens <= parsed_limits.tokens
        request_fit = requests <= parsed_limits.requests
        payload = {"kind": kind, "snapshot_id": snapshot_id,
                   "snapshot_sha256": snapshot["sha256"], "task_sha256": self.suite.sha256,
                   "snapshot_harness_sha256": snapshot["harness_sha256"],
                   "model_fingerprint": cfg.fingerprint, "code_fingerprint": code_fingerprint(),
                   "limits": parsed_limits.__dict__, "estimated_requests": requests,
                   "estimated_reserved_tokens": estimated_tokens, "token_fit_estimate": token_fit,
                   "request_fit_estimate": request_fit,
                   "allow_hooks": allow_hooks, **inputs, "created": time.time(),
                   "expires": time.time() + 1200, "status": "review"}
        sha = digest(payload)
        token = secrets.token_urlsafe(32)
        payload.update({"sha256": sha, "approval_hash": hashlib.sha256(token.encode()).hexdigest()})
        plan_id = self.store.create("plan", payload)
        return {"plan_id": plan_id, "plan_sha256": sha, "approval_token": token,
                "kind": kind, "model": cfg.public(), "estimated_requests": requests,
                "estimated_reserved_tokens": estimated_tokens, "limits": parsed_limits.__dict__,
                "token_fit_estimate": token_fit, "request_fit_estimate": request_fit,
                "price_usd": None,
                "warning": "费用无法由当前配置推导；token 估算与供应商计费口径可能不同。"
                "启动代表接受供应商实际费用及模型请求发送项目上下文。"}

    def start(self, plan_id: str, plan_sha256: str, approval_token: str) -> dict:
        self.store.claim(plan_id, plan_sha256, approval_token)
        plan = self.store.get("plan", plan_id)
        if not plan["token_fit_estimate"] or not plan["request_fit_estimate"]:
            raise ValueError("审批预算低于估算；请提高预算后重新生成计划")
        job = {key: value for key, value in plan.items()
               if key not in {"approval_hash", "approval_token"}}
        job.update({"status": "queued", "plan_id": plan_id, "plan_sha256": plan_sha256,
                    "started": time.time(), "results": {}, "errors": []})
        job_id = uuid.uuid4().hex
        job["job_id"] = job_id
        self.store.create("job", job, ident=job_id)
        self._launch(job_id)
        return {"job_id": job_id, "status": "queued", "kind": job["kind"],
                "estimated_requests": job["estimated_requests"]}

    def resume(self, job_id: str) -> dict:
        job = self.store.get("job", job_id)
        if job["status"] not in {"interrupted", "failed", "cancelled"}:
            raise ValueError("只有已中断或失败的作业可以恢复")
        job["status"] = "queued"
        job["resumed"] = time.time()
        self.store.put("job", job_id, job, action="job.resumed")
        self._launch(job_id)
        return {"job_id": job_id, "status": "queued"}

    def cancel(self, job_id: str) -> dict:
        budget = self._budgets.get(job_id)
        if budget is None:
            job = self.store.get("job", job_id)
            if job["status"] not in {"queued", "running"}:
                return {"job_id": job_id, "status": job["status"]}
            job["cancel_requested"] = True
            self.store.put("job", job_id, job, action="job.cancel_requested")
            return {"job_id": job_id, "status": "cancel_requested"}
        budget.cancelled.set()
        return {"job_id": job_id, "status": "cancel_requested"}

    def status(self, job_id: str = "") -> dict:
        if job_id:
            job = self.store.get("job", job_id)
            thread = self._threads.get(job_id)
            if job.get("status") == "running" and (thread is None or not thread.is_alive()):
                job["status"] = "interrupted"
                job["interrupted"] = time.time()
                self.store.put("job", job_id, job, action="job.interrupted")
            return self._public_job(job_id, job)
        return {"jobs": [self._public_job(ident, job)
                         for ident, job in reversed(self.store.list("job"))[-30:]]}

    def report(self, job_id: str) -> dict:
        job = self.store.get("job", job_id)
        result = self._public_job(job_id, job)
        if job.get("kind") == "optimize":
            result["candidates"] = job.get("candidates", [])
            result["selected_candidate"] = job.get("selected_candidate")
        return result

    def preview_apply(self, job_id: str, candidate_id: str) -> dict:
        job = self.store.get("job", job_id)
        if job.get("kind") != "optimize" or job.get("status") != "complete":
            raise ValueError("候选作业尚未完成")
        candidate = self._candidate(job, candidate_id)
        if candidate_id != job.get("selected_candidate"):
            raise ValueError("只允许预览 validation 阶段冻结的候选")
        if not candidate.get("recommended"):
            raise ValueError("验证数据未证明候选优于基线，不能应用")
        snapshot = self.store.get("snapshot", job["snapshot_id"])
        root = Path(snapshot["project_root"])
        before = inventory(root, harness_only=True, skip_binary=True)
        if digest(hashes(before)) != job["snapshot_harness_sha256"]:
            raise ValueError("项目 Harness 文件已变化；请重新扫描并运行实验")
        after = self._candidate_tree(job, candidate_id)
        from .native import validate
        validate(after, hooks_approved=job["allow_hooks"])
        diff = self._diff(before, after)
        approval = secrets.token_urlsafe(32)
        plan = {"kind": "apply", "job_id": job_id, "candidate_id": candidate_id,
                "root": str(root), "before": before, "after": after,
                "before_sha256": digest(hashes(before)), "after_sha256": digest(hashes(after)),
                "created": time.time(), "expires": time.time() + 1200, "status": "review",
                "approval_hash": hashlib.sha256(approval.encode()).hexdigest()}
        plan_sha = digest({key: value for key, value in plan.items()
                           if key not in {"approval_hash", "status"}})
        plan["sha256"] = plan_sha
        plan_id = self.store.create("plan", plan)
        return {"plan_id": plan_id, "plan_sha256": plan_sha, "approval_token": approval,
                "files_before": len(before), "files_after": len(after), "diff": diff[:30000],
                "expires_in_seconds": 1200,
                "warning": "再次调用 hp_apply_candidate 会修改项目文件，并保留本地回滚副本。"}

    def apply(self, plan_id: str, plan_sha256: str, approval_token: str) -> dict:
        self._recover_transactions()
        self.store.claim(plan_id, plan_sha256, approval_token)
        plan = self.store.get("plan", plan_id)
        if plan.get("kind") not in {"apply", "rollback"}:
            raise ValueError("审批计划操作类型无效")
        root = Path(plan["root"])
        current = inventory(root, harness_only=True, skip_binary=True)
        if digest(hashes(current)) != plan["before_sha256"]:
            raise ValueError("项目 Harness 文件在预览后发生变化，计划已失效")
        transaction_id = uuid.uuid4().hex
        transaction = {"id": transaction_id, "root": str(root), "before": plan["before"],
                       "after": plan["after"], "status": "applying", "created": time.time()}
        self._save_transaction(transaction)
        self._replace_tree(root, plan["before"], plan["after"])
        transaction["status"] = "applied"
        self._save_transaction(transaction)
        record = {"transaction_id": transaction_id, "job_id": plan.get("job_id"),
                  "candidate_id": plan.get("candidate_id"), "root": str(root),
                  "before_sha256": plan["before_sha256"], "after_sha256": plan["after_sha256"],
                  "created": time.time(), "status": "applied"}
        app_id = self.store.create("application", record)
        if plan["kind"] == "rollback":
            original = self.store.get("application", plan["application_id"])
            original["status"] = "rolled_back"
            original["rolled_back_by"] = app_id
            self.store.put("application", plan["application_id"], original,
                           action="application.rolled_back")
            transaction = self._load_transaction(plan["transaction_id"])
            transaction["status"] = "rolled_back"
            self._save_transaction(transaction)
        return {"application_id": app_id, "transaction_id": transaction_id,
                "status": "rolled_back" if plan["kind"] == "rollback" else "applied",
                "files": len(plan["after"]),
                "rollback_available": True}

    def preview_rollback(self, application_id: str) -> dict:
        record = self.store.get("application", application_id)
        transaction = self._load_transaction(record["transaction_id"])
        if transaction["status"] != "applied":
            raise ValueError("应用记录已回滚或未完成")
        root = Path(record["root"])
        current = inventory(root, harness_only=True, skip_binary=True)
        if digest(hashes(current)) != record["after_sha256"]:
            raise ValueError("项目 Harness 文件已变化；拒绝覆盖后回滚")
        approval = secrets.token_urlsafe(32)
        plan = {"kind": "rollback", "application_id": application_id,
                "transaction_id": record["transaction_id"], "root": str(root),
                "before": current, "after": transaction["before"],
                "before_sha256": digest(hashes(current)),
                "after_sha256": digest(hashes(transaction["before"])),
                "created": time.time(), "expires": time.time() + 1200, "status": "review",
                "approval_hash": hashlib.sha256(approval.encode()).hexdigest()}
        plan_sha = digest({key: value for key, value in plan.items()
                           if key not in {"approval_hash", "status"}})
        plan["sha256"] = plan_sha
        plan_id = self.store.create("plan", plan)
        return {"plan_id": plan_id, "plan_sha256": plan_sha, "approval_token": approval,
                "diff": self._diff(current, transaction["before"])[:30000],
                "warning": "执行此计划会恢复应用前的 Harness 文件。"}

    def _launch(self, job_id: str):
        with self._lock:
            current = self._threads.get(job_id)
            if current and current.is_alive():
                return
            thread = threading.Thread(target=self._run, args=(job_id,), daemon=True,
                                      name=f"harnesspilot-{job_id[:8]}")
            self._threads[job_id] = thread
            thread.start()

    def _run(self, job_id: str):
        job_dir = self.store.artifact("jobs", job_id)
        lock_path = job_dir / "worker.lock"
        lock_file = lock_path.open("a+b")
        try:
            if fcntl:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except BlockingIOError:
            lock_file.close()
            return
        try:
            job = self.store.get("job", job_id)
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                self.store.put("job", job_id, job, action="job.cancelled")
                return
            job.update({"status": "running", "pid": os.getpid(), "heartbeat": time.time()})
            self.store.put("job", job_id, job, action="job.started")
            snapshot = self.store.get("snapshot", job["snapshot_id"])
            frozen = self._frozen(snapshot)
            for key in ("snapshot_sha256", "task_sha256", "model_fingerprint", "code_fingerprint"):
                frozen_key = {"snapshot_sha256": "snapshot_sha256", "task_sha256": "task_sha256",
                              "model_fingerprint": "model_fingerprint",
                              "code_fingerprint": "code_fingerprint"}[key]
                if frozen[key] != job[frozen_key]:
                    raise ValueError(f"冻结作业指纹发生变化：{key}")
            cfg = ModelConfig.load()
            prior = job.get("budget")
            budget = Budget(Limits(**job["limits"]),
                            persist=lambda value: self._save_budget(job_id, value), prior=prior)
            self._budgets[job_id] = budget
            sandbox = Sandbox(cfg, budget)
            with tempfile.TemporaryDirectory(prefix="harnesspilot-broker-") as broker_dir:
                broker_root = Path(broker_dir)
                if os.geteuid() == 0:
                    os.chown(broker_root, sandbox.uid, sandbox.gid)
                broker_root.chmod(0o700)
                with Broker(broker_root / "model.sock", cfg, budget,
                            socket_uid=sandbox.uid, socket_gid=sandbox.gid) as broker:
                    sandbox.broker = broker
                    if job["kind"] == "baseline":
                        self._baseline(job_id, job, snapshot, sandbox, job_dir)
                    elif job["kind"] == "optimize":
                        self._optimize(job_id, job, snapshot, sandbox, job_dir)
                    elif job["kind"] == "sealed":
                        self._sealed(job_id, job, snapshot, sandbox, job_dir)
                    else:
                        raise ValueError("作业类型无效")
            job = self.store.get("job", job_id)
            job.update({"status": "complete", "finished": time.time(),
                        "budget": budget.public(), "pid": None})
            self.store.put("job", job_id, job, action="job.completed")
        except BaseException as exc:
            try:
                job = self.store.get("job", job_id)
                job.update({"status": "cancelled" if isinstance(exc, Exception)
                            and str(exc) == "实验已取消" else "failed",
                            "finished": time.time(), "pid": None,
                            "errors": (job.get("errors", []) + [redact_text(
                                f"{type(exc).__name__}: {exc}",
                                cfg.secrets() if "cfg" in locals() else [])])[:20],
                            "traceback": redact_text(
                                traceback.format_exc()[-4000:],
                                cfg.secrets() if "cfg" in locals() else [])})
                self.store.put("job", job_id, job, action="job.failed")
            except Exception:
                pass
        finally:
            self._budgets.pop(job_id, None)
            if fcntl:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            else:
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            lock_file.close()

    def _baseline(self, job_id, job, snapshot, sandbox, job_dir):
        tree = self._load_snapshot(job["snapshot_id"])
        adapter = ProjectAdapter(sandbox, self.suite, tree, job_dir / "adapter",
                                 hooks_approved=job["allow_hooks"])
        results = job.setdefault("results", {})
        for task_id in job["task_ids"]:
            self._check_cancel(job_id)
            if task_id in results:
                continue
            run = adapter.execute(task_id, adapter.base_harness(), f"{job_id}/baseline/{task_id}")
            score = adapter.score(run, task_id)
            results[task_id] = {"baseline": score, "run": str(run)}
            job["heartbeat"] = time.time()
            self.store.put("job", job_id, job, action="job.task.completed")
        job["summary"] = {"tasks": len(results),
                          "passed": sum(row["baseline"].get("passed", False)
                                         for row in results.values()),
                          "split": "train+validation"}
        self.store.put("job", job_id, job, action="job.baseline.completed")

    def _optimize(self, job_id, job, snapshot, sandbox, job_dir):
        from harness2.core.schedule import run_domain

        baseline = self.store.get("job", job["baseline_job"])
        if baseline["status"] != "complete":
            raise ValueError("基线作业已不完整")
        tree = self._load_snapshot(job["snapshot_id"])
        adapter = ProjectAdapter(sandbox, self.suite, tree, job_dir / "adapter",
                                 hooks_approved=job["allow_hooks"])
        proposer = CodexProposer(sandbox, job_dir / "proposer", adapter.channel)
        h2_root = job_dir / "harness2"
        for domain in sorted({adapter.domain_of(task) for task in job["train_ids"]}):
            task_ids = [task for task in job["train_ids"] if adapter.domain_of(task) == domain]
            self._check_cancel(job_id)
            result = run_domain(domain, task_ids, job["k"], adapter=adapter, prop=proposer,
                                runs_root=h2_root / "runs", library_root=h2_root / "libraries",
                                modes=("agg", "seq"), seed=42, tag=job_id[:8])
            job.setdefault("domains", []).append(result)
            self.store.put("job", job_id, job, action="job.domain.completed")
        candidates = self._collect_candidates(job, adapter, h2_root / "runs", job_dir)
        if not candidates:
            raise ValueError("Harness² 未产生可复核的候选树")
        validation = self.suite.ids("validation")
        baseline_rows = baseline["results"]
        cap = 4
        candidates.sort(key=lambda row: (-row["frequency"], row["sha256"]))
        candidates = candidates[:cap]
        outcomes = []
        for candidate in candidates:
            scores = {}
            for task_id in validation:
                self._check_cancel(job_id)
                run = adapter.execute(task_id, self._candidate_tree(job, candidate["id"]),
                                      f"{job_id}/validation/{candidate['id']}/{task_id}")
                scores[task_id] = adapter.score(run, task_id)
            rows = [{"family": self.suite.tasks[task]["family"],
                     "baseline": baseline_rows[task]["baseline"].get("passed", False),
                     "candidate": scores[task].get("passed", False)} for task in validation]
            stats = paired(rows)
            candidate.update({"validation": scores, "statistics": stats,
                              "recommended": stats["wins"] > 0 and stats["regressions"] == 0
                              and stats["delta"] > 0 and stats["ci95"][0] > 0})
            outcomes.append(candidate)
            self.store.put("job", job_id, job, action="job.candidate.validated")
        eligible = [c for c in outcomes if c["recommended"]]
        selected = max(eligible, key=lambda c: (c["statistics"]["delta"],
                                               c["statistics"]["ci95"][0],
                                               -c["changed_files"], c["sha256"])) if eligible else None
        job.update({"candidates": [{key: value for key, value in row.items() if key != "tree"}
                                   for row in outcomes],
                    "selected_candidate": selected["id"] if selected else None,
                    "frozen_validation": True,
                    "snapshot_harness_sha256": snapshot["harness_sha256"],
                    "summary": {"train_tasks": len(job["train_ids"]),
                                "validation_tasks": len(validation),
                                "candidate_count": len(outcomes),
                                "recommended_candidate": selected["id"] if selected else None,
                                "claim": "验证集结果不构成泛化性能证明"}})
        self.store.put("job", job_id, job, action="job.validation.frozen")

    def _sealed(self, job_id, job, snapshot, sandbox, job_dir):
        optimization = self.store.get("job", job["optimization_job"])
        candidate_id = job["candidate_id"]
        if (optimization.get("selected_candidate") != candidate_id
                or not optimization.get("frozen_validation")):
            raise ValueError("候选 validation 冻结身份不匹配")
        if optimization.get("sealed_complete"):
            raise ValueError("该候选已完成 sealed 复评")
        tree = self._load_snapshot(job["snapshot_id"])
        adapter = ProjectAdapter(sandbox, self.suite, tree, job_dir / "adapter",
                                 hooks_approved=job["allow_hooks"])
        baseline = self.store.get("job", optimization["baseline_job"])
        results = {}
        candidate_tree = self._candidate_tree(optimization, candidate_id)
        for task_id in self.suite.ids("sealed"):
            self._check_cancel(job_id)
            base_run = adapter.execute(task_id, adapter.base_harness(),
                                       f"{job_id}/sealed/base/{task_id}")
            cand_run = adapter.execute(task_id, candidate_tree,
                                       f"{job_id}/sealed/{candidate_id}/{task_id}")
            results[task_id] = {"baseline": adapter.score(base_run, task_id),
                                "candidate": adapter.score(cand_run, task_id)}
            job["results"] = results
            self.store.put("job", job_id, job, action="job.sealed.task.completed")
        rows = [{"family": self.suite.tasks[task]["family"],
                 "baseline": value["baseline"].get("passed", False),
                 "candidate": value["candidate"].get("passed", False)}
                for task, value in results.items()]
        job["summary"] = paired(rows)
        optimization["sealed_complete"] = True
        optimization["sealed_job_id"] = job_id
        self.store.put("job", job["optimization_job"], optimization,
                       action="job.sealed.completed")
        self.store.put("job", job_id, job, action="job.sealed.summary")

    def _collect_candidates(self, job, adapter, runs_root, job_dir):
        from harness2.core.tree import read_tree
        from harness2.core.schedule import Namespace

        counts = {}
        for summary_path in runs_root.rglob("summary.json"):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                task_id = summary.get("task_id") or summary_path.parent.name
                if task_id not in job["train_ids"]:
                    continue
                for mode, selection in summary.get("selections", {}).items():
                    label = selection.get("chosen_label")
                    if not label:
                        continue
                    namespace = Namespace("harnesspilot", "cx", mode, job["k"], seed=42,
                                          tag=job["job_id"][:8])
                    path = namespace.task_dir(runs_root, task_id) / "harnesses" / label
                    candidate_tree = read_tree(adapter.substrate.surface(), path)
                    key = digest(candidate_tree)
                    counts.setdefault(key, {"tree": candidate_tree, "frequency": 0,
                                            "source_tasks": [], "modes": []})
                    counts[key]["frequency"] += 1
                    counts[key]["source_tasks"].append(task_id)
                    counts[key]["modes"].append(mode)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        result = []
        for sha, value in sorted(counts.items()):
            ident = uuid.uuid4().hex
            artifact = self.store.artifact("checkpoints", ident)
            self.store.export("tree.json", value["tree"], artifact)
            changed = set(value["tree"]) ^ set(adapter.base)
            changed.update(name for name in set(value["tree"]) & set(adapter.base)
                           if value["tree"][name] != adapter.base[name])
            row = {"id": ident, "sha256": sha, "frequency": value["frequency"],
                   "source_tasks": sorted(set(value["source_tasks"])),
                   "modes": sorted(set(value["modes"])), "changed_files": len(changed),
                   "tree_path": str(artifact / "tree.json"), "recommended": False}
            job.setdefault("candidate_index", {})[ident] = row
            result.append({**row, "tree": value["tree"]})
        return result

    def _candidate(self, job, candidate_id):
        for candidate in job.get("candidates", []):
            if candidate.get("id") == candidate_id:
                return candidate
        row = job.get("candidate_index", {}).get(candidate_id)
        if row:
            return row
        raise ValueError("候选不存在")

    def _candidate_tree(self, job, candidate_id):
        candidate = self._candidate(job, candidate_id)
        return json.loads(Path(candidate["tree_path"]).read_text(encoding="utf-8"))

    def _load_snapshot(self, ident):
        record = self.store.get("snapshot", ident)
        return json.loads(Path(record["snapshot_path"]).read_text(encoding="utf-8"))

    def _frozen(self, snapshot):
        cfg = ModelConfig.load()
        return {"snapshot_sha256": snapshot["sha256"], "task_sha256": self.suite.sha256,
                "model_fingerprint": cfg.fingerprint, "code_fingerprint": code_fingerprint()}

    def _save_budget(self, job_id, value):
        job = self.store.get("job", job_id)
        job["budget"] = value
        job["heartbeat"] = time.time()
        self.store.put("job", job_id, job)

    def _check_cancel(self, job_id):
        job = self.store.get("job", job_id)
        if job.get("cancel_requested"):
            budget = self._budgets.get(job_id)
            if budget:
                budget.cancelled.set()
            raise RuntimeError("实验已取消")

    def _public_job(self, job_id, job):
        result = {key: job.get(key) for key in ("kind", "status", "snapshot_id",
                  "started", "finished", "heartbeat", "estimated_requests", "budget",
                  "summary", "errors", "candidates", "selected_candidate", "sealed_complete")}
        result["job_id"] = job_id
        return result
        
    @staticmethod
    def _diff(before, after):
        lines = []
        for name in sorted(set(before) | set(after)):
            lines.extend(difflib.unified_diff(before.get(name, "").splitlines(keepends=True),
                after.get(name, "").splitlines(keepends=True), fromfile=f"a/{name}",
                tofile=f"b/{name}"))
        return "".join(lines) or "候选与当前 Harness 内容相同。"

    def _transaction_dir(self):
        path = self.store.root / "transactions"
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def _save_transaction(self, transaction):
        destination = self._transaction_dir()
        self.store.export(transaction["id"] + ".json", transaction, destination)

    def _load_transaction(self, ident):
        return json.loads((self._transaction_dir() / f"{ident}.json").read_text(encoding="utf-8"))

    @staticmethod
    def _replace_tree(root, before, after):
        with SecureRoot(root) as secure:
            for name in sorted(set(before) | set(after)):
                value = after[name].encode("utf-8") if name in after else None
                secure.replace(name, value)

    def _recover_transactions(self):
        directory = self._transaction_dir()
        for path in directory.glob("*.json"):
            try:
                transaction = json.loads(path.read_text(encoding="utf-8"))
                if transaction.get("status") == "applying":
                    self._replace_tree(Path(transaction["root"]), transaction["after"],
                                       transaction["before"])
                    transaction["status"] = "recovered_rollback"
                    self._save_transaction(transaction)
            except (OSError, ValueError, KeyError):
                continue
