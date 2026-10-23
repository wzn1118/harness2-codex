#!/usr/bin/env python3
"""Deterministic offline judge smoke tests; never launches Codex or Harness²."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


TASKS = {
    "discount": {
        "bug": "def net_price(price, discount):\n    return round(price * (1 + discount), 2)\n",
        "fixed": "def net_price(price, discount):\n    return round(price * (1 - discount), 2)\n",
        "judge": "from ops import net_price\nassert net_price(100, .2) == 80\nassert net_price(50, 0) == 50\nassert net_price(10, 1) == 0\n",
    },
    "slug": {
        "bug": "def slugify(text):\n    return text.lower().replace(' ', '_')\n",
        "fixed": "import re\ndef slugify(text):\n    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', text.strip().lower())).strip('-')\n",
        "judge": "from ops import slugify\nassert slugify('Hello World!') == 'hello-world'\nassert slugify(' A---B  C ') == 'a-b-c'\nassert slugify('...') == ''\n",
    },
    "clamp": {
        "bug": "def clamp(value, lower, upper):\n    return max(upper, min(lower, value))\n",
        "fixed": "def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError('lower must be <= upper')\n    return max(lower, min(upper, value))\n",
        "judge": "from ops import clamp\nassert clamp(5, 0, 10) == 5\nassert clamp(-1, 0, 10) == 0\nassert clamp(11, 0, 10) == 10\ntry:\n    clamp(1, 3, 2)\nexcept ValueError:\n    pass\nelse:\n    raise AssertionError('reversed bounds must raise')\n",
    },
}


def run_judge(workspace: Path, key: str) -> bool:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    code = "import sys; sys.path.insert(0, '.');\n" + TASKS[key]["judge"]
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=workspace, env=env,
        capture_output=True, text=True, timeout=15, check=False,
    )
    return result.returncode == 0


def offline() -> None:
    with tempfile.TemporaryDirectory(prefix="harnesspilot-offline-") as temp:
        root = Path(temp)
        for key, task in TASKS.items():
            results = []
            for variant in ("bug", "fixed"):
                work = root / f"{key}-{variant}"
                work.mkdir()
                (work / "ops.py").write_text(task[variant], encoding="utf-8")
                results.append(run_judge(work, key))
            if results != [False, True]:
                raise AssertionError(f"{key}: expected buggy/fixed outcomes [false, true], got {results}")
            print(f"PASS {key}: baseline fails; known fix passes")
    print("PASS: 3 deterministic fixtures, no Docker, no credentials, no model calls")


def ab(args) -> None:
    keys = args.tasks.split(",")
    if not keys or any(key not in TASKS for key in keys):
        raise SystemExit("--tasks must be comma-separated values from " + ",".join(TASKS))
    if args.repeats < 1 or args.timeout < 1 or args.max_calls < 1:
        raise SystemExit("repeats, timeout and max-calls must be positive")
    calls = len(keys) * args.repeats * 2
    if calls > args.max_calls:
        raise SystemExit(f"Refusing {calls} planned model sessions; max-calls is {args.max_calls}")
    if args.live:
        raise SystemExit("This testkit intentionally has no live runner. Use the plugin's harness2_run for the sandboxed real Harness² evaluation.")
    candidate = Path(args.candidate_file).expanduser().resolve() if args.candidate_file else None
    if candidate and not candidate.is_file():
        raise SystemExit(f"Candidate file not found: {candidate}")
    print(json.dumps({
        "status": "DRY_RUN_NO_MODEL_CALLS",
        "tasks": keys,
        "repeats": args.repeats,
        "planned_sessions": calls,
        "candidate": str(candidate) if candidate else None,
        "timeout_seconds_each": args.timeout,
        "docker": False,
        "harness2": False,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("offline")
    plan = sub.add_parser("ab")
    plan.add_argument("--candidate-file")
    plan.add_argument("--tasks", default=",".join(TASKS))
    plan.add_argument("--repeats", type=int, default=1)
    plan.add_argument("--max-calls", type=int, default=6)
    plan.add_argument("--timeout", type=int, default=180)
    plan.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.command == "preflight":
        print(f"Python: {sys.version.split()[0]} ({sys.platform})")
        print(f"Git: {shutil.which('git') or 'not installed'}")
        print("Docker: unused")
        print("Codex: intentionally not invoked by this deterministic testkit")
    elif args.command == "offline":
        offline()
    else:
        ab(args)


if __name__ == "__main__":
    main()
