"""确定性任务合同与宿主判卷；期望结果始终留在沙箱外。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import ROOT, digest
from .files import relative

NODE = shutil.which("node") or "/usr/bin/node"


class Suite:
    def __init__(self, path: Path | None = None):
        self.path = (path or ROOT / "benchmarks/smoke.json").resolve()
        if self.path.stat().st_size > 4_194_304:
            raise ValueError("任务清单过大")
        self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(self.data, dict) or set(self.data) != {"version", "tasks"}:
            raise ValueError("任务清单顶层字段无效")
        if self.data["version"] != 1 or not isinstance(self.data["tasks"], list):
            raise ValueError("任务清单版本无效")
        self.tasks = {}
        families = {}
        for task in self.data["tasks"]:
            if not isinstance(task, dict) or set(task) - {
                    "id", "kind", "split", "family", "prompt", "cases", "sources"}:
                raise ValueError("任务字段无效")
            ident = task["id"]
            if (not isinstance(ident, str) or ident in self.tasks
                    or not ident.replace("-", "").isalnum()):
                raise ValueError("任务名称无效或重复")
            if task["kind"] not in {"python", "node", "rust"}:
                raise ValueError("任务语言未支持")
            if task["split"] not in {"train", "validation", "sealed"}:
                raise ValueError("任务 split 无效")
            family = task["family"]
            if not isinstance(family, str) or not family.strip():
                raise ValueError("任务问题家族无效")
            if family in families and families[family] != task["split"]:
                raise ValueError("同一问题家族不得跨 split")
            families[family] = task["split"]
            if (not isinstance(task["prompt"], str) or not task["prompt"].strip()
                    or not isinstance(task["cases"], list) or not task["cases"]):
                raise ValueError("任务缺少提示词或测试样本")
            for case in task["cases"]:
                if not isinstance(case, dict) or set(case) != {"input", "expected"}:
                    raise ValueError("测试样本需要 input 与 expected")
            for name, text in task.get("sources", {}).items():
                relative(name)
                if (not isinstance(text, str)
                        or name.startswith((".codex/", ".agents/", "sub_agents/"))):
                    raise ValueError("任务源码字段无效")
            self.tasks[ident] = task
        if not self.tasks:
            raise ValueError("任务清单为空")
        self.sha256 = digest(self.data)

    def ids(self, split="all"):
        return sorted(t for t, task in self.tasks.items()
                      if split == "all" or task["split"] == split)

    def public(self):
        return {"sha256": self.sha256, "tasks": [
            {key: task[key] for key in ("id", "kind", "split", "family")}
            for task in self.tasks.values()], "source": str(self.path)}

    def public_task(self, task_id: str) -> dict:
        task = self.tasks[task_id]
        result = {key: task[key] for key in ("id", "kind", "split", "family")}
        if task["split"] != "sealed":
            result["prompt"] = task["prompt"]
        return result


def judge(sandbox, task: dict, files: dict[str, str]) -> dict:
    cases = [c["input"] for c in task["cases"]]
    files = {name: value for name, value in files.items()
             if not name.startswith(".codex/") and name != ".hp-input.json"}
    files[".hp-input.json"] = json.dumps(cases)
    kind = task["kind"]
    if kind == "python":
        code = ("import sys,json,importlib.util; "
                "s=importlib.util.spec_from_file_location('candidate','/workspace/solution.py'); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "v=json.load(open('/workspace/.hp-input.json')); "
                "r=[m.solve(x) for x in v]; print(json.dumps(r,allow_nan=False))")
        argv = ["/usr/bin/python3", "-I", "-c", code]
    elif kind == "node":
        code = ("const fs=require('fs'),m=require('/workspace/solution.js');"
                "const x=JSON.parse(fs.readFileSync('/workspace/.hp-input.json','utf8'));"
                "console.log(JSON.stringify(x.map(v=>m.solve(v))));")
        argv = [NODE, "-e", code]
    else:
        wrapper = ('mod candidate { include!("/workspace/solution.rs"); }\n'
                   'fn main() { let s=std::env::args().nth(1).unwrap(); '
                   'println!("{}", candidate::solve(&s)); }\n')
        files[".hp-main.rs"] = wrapper
        code = ("import json,subprocess; "
                "subprocess.run(['/usr/bin/rustc','--crate-name','hp_run','/workspace/.hp-main.rs',"
                "'-o','/tmp/hp-run'],check=True); "
                "v=json.load(open('/workspace/.hp-input.json')); "
                "r=[json.loads(subprocess.check_output(['/tmp/hp-run',str(x)],text=True)) for x in v]; "
                "print(json.dumps(r))")
        argv = ["/usr/bin/python3", "-I", "-c", code]
    result = sandbox.run(files, argv, timeout=45, export=False)
    try:
        actual = json.loads(result.stdout)
        expected = [c["expected"] for c in task["cases"]]
        # 结构和类型同时核对，拒绝布尔值与数字之间的隐式相等。
        passed = result.returncode == 0 and digest(actual) == digest(expected)
    except (ValueError, TypeError):
        passed = False
    return {"passed": passed, "returncode": result.returncode, "seconds": result.seconds,
            "judge": "host-exact-json-v1", "cases": len(cases),
            "error": result.stderr[-2000:] if not passed else ""}
