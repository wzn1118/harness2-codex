# Harness² for Codex

非官方 Codex 插件：在 Codex 聊天中调用 Google Research Harness² 的真实
Propose–Probe–Compose 流程，为项目级 `AGENTS.md`、Skills 和受限的工具/执行选项
做隔离实验，并记录独立验证、差异、执行轨迹和 token 成本。

## 插件

仓库提供两个互补插件：

- `harness2-codex`：直接暴露 Harness² 实验、验证、应用与回滚流程。
- `harnesspilot`：提供项目扫描、预算冻结、baseline、agg/seq 优化、独立验证、报告与审查门控。

## 安装

需要支持插件与 MCP 的 Codex CLI、Linux、Python 3.10+、可用的 bubblewrap 用户命名空间，
以及已配置 API key 的 Codex Responses provider。应用或回滚还需要可显示原生确认框的
Linux 图形会话。OAuth-only 登录不受支持；缺少隔离前提时插件会拒绝运行，不降级沙箱。

```bash
codex plugin marketplace add wzn1118/harness2-codex
codex plugin add harness2-codex@harness2-local
codex plugin add harnesspilot@harness2-local
codex plugin list
```

安装后开启一个新聊天。HarnessPilot 的完整安装教程、隔离边界、预算参数和实测限制见
[HarnessPilot 安装文档](plugins/harnesspilot/docs/INSTALLATION.md)。Harness² 原生插件的中文教程见
[插件使用教程](plugins/harness2-codex/docs/USAGE.zh-CN.md)。

## 首次实验

在聊天中调用 `$harness2-optimize` 或 `$harnesspilot`，提供项目的绝对路径与优化目标。先让 Codex
检查可用测试、展示建议的 `check_argv` 和实验预算；确认后再运行 `k=1`，并要求
只提交对照结果和差异、不应用修改。Harness² 不会把提示词模拟当成实验：插件调用
固定上游核心并启动真实隔离的 Codex 会话。

实验对照有效且候选通过独立验证后，应用仍必须由你明确要求，并在系统原生确认框中
亲自确认。没有有效差异或对照失败时不会产生应用令牌。

## 项目状态

此集成属于社区维护项目，未获得 Google 或 OpenAI 官方背书，也不修改 Codex UI/可执行文件。当前为 Linux
实验版；上游来源、验证记录与已知限制见
[集成审计](plugins/harness2-codex/docs/INTEGRATION_AUDIT.md)。
