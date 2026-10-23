from __future__ import annotations

import json
import re

import tomlkit
import yaml

EVENTS = {"PreToolUse", "PostToolUse", "PermissionRequest", "PreCompact", "PostCompact",
          "SessionStart", "SubagentStart", "SubagentStop", "UserPromptSubmit", "Stop"}


def frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", text, re.S)
    if not match:
        raise ValueError("角色与 Skill 需要 YAML frontmatter")
    metadata = yaml.safe_load(match[1])
    if not isinstance(metadata, dict) or not isinstance(metadata.get("description"), str):
        raise ValueError("frontmatter 需要 description 文本")
    if not metadata["description"].strip() or not match[2].strip():
        raise ValueError("描述和正文均不能为空")
    return metadata, match[2]


def validate(tree: dict[str, str], *, hooks_approved: bool = False):
    from .files import editable

    if not tree.get("AGENTS.md", "").strip() or len(tree["AGENTS.md"]) > 8000:
        raise ValueError("AGENTS.md 需要 1 至 8000 字符")
    for name, text in tree.items():
        if not editable(name):
            raise ValueError(f"候选包含未开放路径：{name}")
        if len(text.encode()) > 1_048_576:
            raise ValueError("候选文件过大")
        if name.endswith(("/SKILL.md", "/AGENT.md")):
            meta, _ = frontmatter(text)
            if "name" in meta and meta["name"] != name.split("/")[-2]:
                raise ValueError("frontmatter 名称需要与目录一致")
        if name.endswith(".py"):
            compile(text, name, "exec")
    if ".codex/hooks.json" in tree:
        if not hooks_approved:
            raise ValueError("候选包含 Hooks；计划需要显式批准 Hooks 执行")
        obj = json.loads(tree[".codex/hooks.json"])
        if not isinstance(obj, dict) or set(obj) != {"hooks"} or not isinstance(obj["hooks"], dict):
            raise ValueError("Hooks 顶层格式无效")
        for event, groups in obj["hooks"].items():
            if event not in EVENTS or not isinstance(groups, list):
                raise ValueError("Hooks 事件无效")
            for group in groups:
                if not isinstance(group, dict) or set(group) - {"matcher", "hooks"}:
                    raise ValueError("Hooks 匹配组格式无效")
                if not isinstance(group.get("hooks"), list) or not group["hooks"]:
                    raise ValueError("Hooks 处理器不能为空")
                for hook in group["hooks"]:
                    if not isinstance(hook, dict) or hook.get("type") != "command":
                        raise ValueError("当前 Codex 仅接入 command 类型 Hooks")
                    if set(hook) - {"type", "command", "timeout", "statusMessage"}:
                        raise ValueError("Hook 含当前运行时未支持的字段")
                    if not isinstance(hook.get("command"), str) or not hook["command"].strip():
                        raise ValueError("Hook 命令不能为空")
                    if "timeout" in hook and (type(hook["timeout"]) is not int
                                               or not 1 <= hook["timeout"] <= 600):
                        raise ValueError("Hook 超时需要在 1 至 600 秒之间")


def compile_config(tree: dict[str, str], original: str = "", *, cwd: str = "/workspace",
                   hook_states: dict | None = None) -> dict[str, str]:
    doc = tomlkit.parse(original)
    generated = {}
    agents = doc.setdefault("agents", {})
    for name, text in sorted(tree.items()):
        if not name.startswith("sub_agents/") or not name.endswith("/AGENT.md"):
            continue
        role = name.split("/")[1]
        if role in {"max_threads", "max_depth", "job_max_runtime_seconds", "default", "worker"}:
            raise ValueError("角色名称与原生保留配置冲突")
        metadata, body = frontmatter(text)
        path = f".codex/harnesspilot/roles/{role}.toml"
        role_doc = tomlkit.document()
        role_doc["developer_instructions"] = body
        generated[path] = tomlkit.dumps(role_doc)
        agents[role] = {"description": metadata["description"],
                        "config_file": f"harnesspilot/roles/{role}.toml"}
    features = doc.setdefault("features", {})
    if generated:
        features["multi_agent"] = True
    if ".codex/hooks.json" in tree:
        features["codex_hooks"] = True
        features["hooks"] = True
        if hook_states:
            state = doc.setdefault("hooks", {}).setdefault("state", {})
            for key, value in hook_states.items():
                state[key.replace("/workspace/", cwd.rstrip("/") + "/", 1)] = value
    if not agents:
        del doc["agents"]
    if not features:
        del doc["features"]
    generated[".codex/config.toml"] = tomlkit.dumps(doc)
    return generated


def states_from_listing(value) -> dict:
    states = {}

    def visit(obj):
        if isinstance(obj, dict):
            key = obj.get("id") or obj.get("key")
            current = obj.get("currentHash") or obj.get("current_hash")
            if isinstance(key, str) and isinstance(current, str):
                states[key] = {"enabled": True, "trusted_hash": current}
            for item in obj.values():
                visit(item)
        elif isinstance(obj, list):
            for item in obj:
                visit(item)

    visit(value)
    return states
