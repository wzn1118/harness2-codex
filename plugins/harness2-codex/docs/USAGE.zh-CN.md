# Harness² Codex 插件使用教程

这是 Google Research Harness² 固定版本的非官方 Codex 集成。插件会调用上游真实的
Propose–Probe–Compose（PPC）代码和 Codex 子进程，不是用提示词扮演 PPC。实验是项目级、
显式启动的；不会在后台监控或静默修改项目。

## 1. 安装

### 前提

- Linux；bubblewrap（`bwrap`）可用，且内核允许非特权 user/mount/network namespace。
- Python 3.10 或更新版本。
- 支持插件和 MCP 的 Codex CLI，以及其原生 Linux 沙箱。
- Codex 已配置 API key-backed Responses provider。插件沿用配置的模型；仅 ChatGPT OAuth
  登录当前不支持。
- 运行实验不要求桌面；应用或回滚需要 Tkinter 和可显示原生确认框的图形会话。

任一隔离或策略前提不满足时，插件会拒绝执行，不会切换到无沙箱模式、修改权限或降低
Codex 审批保护。

### 从 GitHub 安装

```bash
codex plugin marketplace add wzn1118/harness2-codex
codex plugin add harness2-codex@harness2-local
codex plugin list
```

安装完成后新开一个 Codex 聊天，让该会话加载插件 Skill 和 MCP 工具。工具包括只读检查、
启动、查看状态、取消、确认应用和确认回滚。

## 2. 先检查测试与预算

在新聊天中调用 Skill，并明确要求先只读检查，不要立刻启动有成本的实验：

```text
$harness2-optimize
检查 /绝对路径/我的项目。先只读检查可用测试，列出建议的独立验证命令
（check_argv 数组）及覆盖范围/超时，并展示本次实验的 k、模式、请求数、输出
预留和总 Token 预算。等我确认验证命令和预算后再运行。目标是评估项目
AGENTS.md、Skills、受限工具配置和执行策略；先做 k=1。无论结果如何都只展示
对照结果与差异，不应用修改。
```

项目路径必须是该项目的绝对路径，不能用 `/data` 这类包含多个项目的目录。Harness²
要求明确的 `check_argv`，即参数数组而非 shell 命令字符串，例如：

```json
["/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests"]
```

开始计费前，应核对测试是否可信、是否能在隔离工作目录中运行，以及失败时的判定方式。
不要把生产凭据、无权发送的数据或与目标项目无关的文件纳入实验；选中的源码与轨迹会
发给 Codex 当前配置的模型服务商。

## 3. 运行一次 k=1 实验

确认测试命令和预算后，Codex 会依次调用 `harness2_inspect`、`harness2_run` 和
`harness2_status`。典型参数如下；项目、任务和验证命令需按实际情况填写：

```json
{
  "project_root": "/绝对路径/我的项目",
  "task": "改进项目 Skills 中的测试发现与执行策略",
  "k": 1,
  "mode": "parallel",
  "check_argv": ["/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests"],
  "validation_repeats": 2,
  "max_requests": 160,
  "max_output_tokens": 655360,
  "max_total_tokens": 10000000
}
```

`k=1` 的真实上游 PPC 含 `R=4`、`P=3`。默认独立对照为两组交替顺序的基线/候选测试；
测试会在隔离会话中重新运行。验证资源在实验前按会话独立分配，优化阶段不能挪用某一侧
的验证额度。

预算参数是上限，不代表预期消耗：

| 参数 | 默认值 | 允许范围 | 含义 |
| --- | ---: | ---: | --- |
| `max_requests` | 160 | 8–1000 | provider 请求数上限 |
| `max_output_tokens` | 655,360 | 8,192–4,096,000 | 输出预留上限 |
| `max_total_tokens` | 10,000,000 | 100,000–50,000,000 | 输入与输出总 Token 上限 |
| `validation_repeats` | 2 | 2–5 | 独立基线/候选对照组数 |

先用默认或更小预算，只有在你理解额外消耗后才提高。缺失 provider usage 时按保守预留
统计并标为不完整。`cost_usd` 只有在明确提供每百万输入/输出 Token 价格后才估算；这是
估算值而不是账单。即使设置 Token 上限，也不等于美元硬上限，因为服务商内部计费与未
返回 usage 无法由客户端独立核实。

如果请求完整可靠性验证，即使 PPC 最终选 `KEEP_BASE` 也执行相同表面的验证，可设置
`validate_unchanged=true`。这属于 A/A 可靠性检查，不是优化成功，不会生成应用令牌。设置
`execution_timeout_sec` 会给所有会话统一超时，因此该次实验不再评估超时策略本身。

## 4. 查看并判断结果

对运行中的实验调用 `harness2_status`，读取实际阶段、上游账本、验证结果、差异与成本；
需要中止时用 `harness2_cancel`。报告至少应核对：

- PPC 是否真的完成，是否产生可比较的候选。
- `comparison_valid` 和每一组基线/候选验证是否有效、是否全部通过。
- `changed_files`、完整 diff，以及原项目是否保持未变。
- provider 请求数、实际输入/输出 Token、预留量、usage 是否完整、USD 估值是否有价格依据。

`KEEP_BASE`、空差异、provider 错误、预算耗尽、取消或超时都不是优化收益。失败或不完整的
对照只能报告为无结论；不能用通过的单侧测试、上游占位 `usd: 0` 或普通单元测试替代真实
实验结论。签名审计记录中的失效标记会阻止后续应用。

## 5. 应用与回滚

本教程默认只展示报告和差异，不应用任何修改。只有报告有有效对照、候选通过独立验证并
产生确认令牌后，才可在你明确要求时调用 `harness2_apply`。随后必须由你亲自在系统原生
确认框中确认；插件不接受模型传入的布尔值或文字作为确认，也不自动点击确认框。

应用前会再次核对项目快照。若文件已变化，应用会拒绝而不是覆盖。撤销时明确要求调用
`harness2_rollback`，并在原生对话框确认；它会恢复原始字节与模式，但若应用后文件又被
编辑，则拒绝覆盖这些新改动。无图形会话时可以实验，但不能应用或回滚。

## 6. 记录与可优化范围

默认记录目录为 `~/.codex/harness2-runs-v1/`，可用 `HARNESS2_STATE_DIR` 指定。每次运行
包含签名状态、事件与 Codex JSONL 轨迹、输入快照、上游阶段账本、验证结果、provider usage、
差异和部署/回滚日志。记录保存在本机项目之外。

当前支持项目根部 `AGENTS.md`（只允许保留现有内容后追加）、`.agents/skills/<name>/SKILL.md`
及对应新 helper、有限的 `shell`/`apply_patch` 工具开关、`reasoning_effort` 和超时策略。
工具不能超出基线权限。任意 MCP 命令、hooks、权限策略、全局配置、嵌套 `AGENTS.md`、代码
与测试文件不由应用流程修改。项目已有策略不能准确继承时会拒绝运行。

## 本次实测记录

2026-10-22 至 2026-10-23 的实际模型运行中，一次完整 `k=1` PPC（`R=4/P=3`）由上游合成
器选择 `KEEP_BASE`，没有差异，也没有声称优化收益。随后对相同基线/候选表面做的诊断性
A/A 对照完成了四组；第五组候选启动前发生基础设施错误，因而整组比较被签名标记为无效，
不能将前四组解释成完整验证结论。另一次真实 PPC 遇到服务商反复 HTTP 502，没有选出可用
候选，也未执行独立验证，同样没有效果结论或可应用令牌。

完整 PPC 的 provider 账本记录 1,823,845 个 Token，usage 完整；后续失败运行已报告的
764,227 个 Token usage 不完整。没有提供单价，因此不报告美元费用。所有这些运行均未应用
差异。当前源代码的插件测试 24 项、未修改上游测试 5 项通过；这证明测试覆盖的行为，不
能替代一次有效的项目优化对照。更完整的实现审计见 [INTEGRATION_AUDIT.md](INTEGRATION_AUDIT.md)。
