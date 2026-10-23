from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import ModelConfig
from .sandbox import Sandbox
from .suite import Suite


def doctor():
    findings = {"python": sys.version.split()[0], "platform": sys.platform,
                "bwrap": shutil.which("bwrap"), "node": shutil.which("node"),
                "rustc": shutil.which("rustc"), "suite": Suite().public(),
                "upstream_commit": "1cfe254e8d25b15f37048c3730134686abe60335"}
    try:
        config = ModelConfig.load()
        findings["model"] = config.public()
        findings["auth"] = "loaded from the configured Codex auth.json; values withheld"
    except Exception as exc:
        findings["model_error"] = type(exc).__name__
    binary = Path(__import__("os").environ.get(
        "HARNESSPILOT_CODEX_BIN",
        "/opt/harnesspilot/codex-cli/node_modules/.bin/codex"))
    findings["codex_binary"] = str(binary) if binary.is_file() else None
    import json
    try:
        probe = Sandbox().run(
            {}, ["/usr/bin/python3", "-c",
                 "import json,socket; print(json.dumps([n for _,n in socket.if_nameindex()]))"],
            timeout=15,
        )
        interfaces = json.loads(probe.stdout)
        findings["sandbox_probe"] = {"returncode": probe.returncode, "interfaces": interfaces}
        findings["sandbox_isolated"] = probe.returncode == 0 and interfaces == ["lo"]
    except Exception as exc:
        findings["sandbox_probe_error"] = type(exc).__name__
        findings["sandbox_isolated"] = False
    findings["sandbox_ready"] = bool(findings["bwrap"] and findings["node"]
                                    and findings["rustc"] and findings["codex_binary"]
                                    and findings["sandbox_isolated"])
    print(json.dumps(findings, ensure_ascii=False, indent=2))
    return 0 if findings["sandbox_ready"] and "model" in findings else 2


def main():
    parser = argparse.ArgumentParser(prog="harnesspilot")
    parser.add_argument("command", choices=("mcp", "doctor", "upstream-demo"), nargs="?",
                        default="mcp")
    args, rest = parser.parse_known_args()
    if args.command == "doctor":
        return doctor()
    if args.command == "upstream-demo":
        from harness2.cli.demo import main as demo_main
        sys.argv = [sys.argv[0], *rest]
        return demo_main()
    from .server import main as server_main
    server_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
