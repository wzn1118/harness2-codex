# Harness² Codex 插件使用教程

这是 Google Research Harness² 固定版本的非官方 Codex 集成。它实际调用上游
Propose–Probe–Compose（PPC）递归实现，不用提示词模拟。实验由用户明确发起；不会后台监控
项目或自行写回文件。

## 1. 安装与前提

- Linux、Python 3.10+、`bwrap` 和可用的非特权 user/mount/network namespace。
- 支持插件/MCP 的 Codex CLI，以及 API key-backed Responses provider。
- 默认沿用当前 provider/model；也可在单次运行中选择 `config.toml` 已配置的 provider 和
  `model_catalog_json` 中存在的模型。OAuth-only 认证不支持；隔离条件缺失时 fail closed。
- 运行实验不需要图形界面。应用/回滚需要 Tkinter 和原生桌面确认框。

安装后在新 Codex 聊天使用 `$harness2-optimize`。插件不修改 Codex 二进制、全局配置或
审批设置；worker 沙箱不高于 workspace-write，项目原有只读限制会保留。

如需从本地密钥文件使用另一个 provider，在 Codex 启动环境中设置
`HARNESS2_PROVIDER_API_KEY_FILE` 为密钥文件的绝对路径。文件必须由当前 Codex 用户拥有，权限
为 `0600` 或更严格。插件只在宿主 broker 中读取密钥，不会写入 Codex 配置、项目快照、实验
worker 或工具参数；`.mcp.json` 已允许透传该路径变量。provider 本身仍须由用户预先配置在
Codex `config.toml` 中，插件不会替用户改全局配置。

例如，Codex 已存在 `[model_providers.A6API]` 且模型目录包含 `gpt-6.1-sol` 时，可在
`harness2_inspect` 先检查这个组合，再在 `harness2_run` 传入：

```json
{
  "model_provider": "A6API",
  "model": "gpt-6.1-sol"
}
```

A6API provider 配置示例（需由用户自行添加到自己的 Codex 配置；插件不会写入）：

```toml
[model_providers.A6API]
name = "A6API"
base_url = "https://api.a6api.com/v1"
wire_api = "responses"
requires_openai_auth = true
env_key = "A6API_API_KEY"
```

配置的 `model_catalog_json` 会在实验启动时快照，并以只读方式挂载到每个隔离 worker，确保
Codex worker 使用与宿主相同的模型元数据。明确的 worker 瞬时断流或执行超时，最多使用全新隔离
会话/工作副本重试两次；PPC 改进、rollout 和 holdout 都遵守此上限。Responses body 截断
（`IncompleteRead`）最多重发一次，provider HTTP 502/503/504 最多重发两次。瞬时错误耗尽
provider 重试后会打开熔断器；开始全新隔离会话重试，或后续独立任务开始请求时，插件会审计并
复位 502/503/504 熔断，或响应体明确标注的 `upstream_unavailable` 瞬时错误（A6API 可能将其映射为
HTTP 403）。不会因为前一个任务的瞬时故障而永久阻断后续任务。同一失败会话仍受重试上限约束；
普通认证失败、配额耗尽等其他 HTTP 错误不重试、不复位，继续 fail closed。每次重发都会占用原调用或
holdout arm 的 request/Token 预算并写入签名账本；首次失败、重试原因、熔断复位和最终状态都会保留。
恢复传输不等于任务成功，任务仍按最终输出及独立裁判判分。重复故障或预算不足会使实验保持
inconclusive/invalid；本地预算拒绝会单独记为 broker 错误，不会伪装成上游 HTTP 502。重试使用递增
退避。上游服务可用性无法由插件保证；不会应用差异或伪报优化结果。

接口上报用量超过单次预估，但仍在该任务/对照组原有配额内时，按实际用量补记并继续，不再把
估算偏差当成执行失败。若实际上报超过该组配额，仍完整记账并停止后续支出，不借用其他组额度。
缺失 usage 的请求保留预留，不能按零成本计算；核算调整见账本的 `usage_adjustments`。

## 2. 先检查测试和预算

先明确要求只读检查，不要马上启动模型调用：

```text
$harness2-optimize
检查 /绝对路径/我的项目。先只读检查环境和可优化面；帮我设计 10 个来自不同问题家族的
held-out 任务，每个任务使用仓库中独立的 verifier 文件。展示 check_argv、protected_paths、
任务覆盖面，以及 k=1、2 次重复的请求/输出/总 token 预留。先调用预算估算，等我确认测试
内容和资源上限后再开始。无论结果如何只展示报告和 diff，不应用修改。
```

先看 `harness2_inspect`，再调用只读 `harness2_estimate_budget`。预算工具不会发模型请求；它
返回 provider request、output-token 和 total-token 的预分配上限，不是美元报价。十个任务、两次
重复时，当前默认上限无法给每个验证 session 分配最低额度，启动会在任何模型请求前拒绝；请
根据估算明确提高预算（例如下面展示的上限）。只有用户核对
任务、测试及预算并明确同意后，才可调用 `harness2_run`。项目路径必须是单一项目的绝对路径。

## 3. 准备独立验证套件

顶层 `task` 必须是 worker 能实际完成的代表性项目任务，例如“修复 `ops.py` 中摄氏转华氏函数，
保留签名，只修改该实现并验证边界值”。不要把“运行 Harness²”“优化 AGENTS.md”作为这个参数：
rollout worker 只做项目任务；只有上游 proposer/composer 负责改进候选 harness。元任务误传会让
worker 尝试嵌套调用实验或触碰禁止的文件，不能产生有用的优化证据。

确认性的效果判断需要至少 10 个**调用方声明的问题家族**。每项包括独立任务 prompt、裁判
命令和仓库内的 `protected_paths`。裁判文件会在所有模型执行副本中隐藏，仅在独立验证命令
运行前恢复；路径必须存在，且不能被多个任务共用。任务家族是否真正独立仍需人审，软件无法
判断两个问题是否源于同一缺陷。hold-out prompts 不能与 PPC 的优化任务重复或只是改写。

验证器使用绝对可执行路径和 argv 数组，不接收 shell 字符串。单个项目测试的旧式请求仍可用于
探索，但会标记为单任务/非确认性，不能支持统计提升或应用。例如：

```json
{
  "project_root": "/absolute/path/to/project",
  "task": "优化项目的 Skills 和执行策略",
  "k": 1,
  "mode": "parallel",
  "validation_repeats": 2,
  "max_requests": 1000,
  "max_output_tokens": 4000000,
  "max_total_tokens": 50000000,
  "validation_tasks": [
    {
      "task_id": "serialization-roundtrip",
      "family": "serialization",
      "task": "修复序列化后 round-trip 丢失默认值的问题，只修改实现文件。",
      "check_argv": ["/usr/bin/python3", "verifiers/check_serialization.py"],
      "protected_paths": ["verifiers/check_serialization.py"]
    }
  ]
}
```

这个 JSON 只展示单个任务对象的形状；实际确认性请求要提供至少 10 个彼此独立、各有唯一
裁判文件的任务对象。裁判命令必须在隔离工作目录可运行。不要将生产凭据、无权发送的数据
或其他项目文件纳入请求；允许的项目源码和轨迹会送往当前 Codex 模型服务商。

## 4. 运行与判断

`k=1` 调用真实上游 Harness² PPC（`R=4/P=3`）。完成后读取 `harness2_status`，再通过
`harness2_get_report` 读取 HMAC 校验的完整报告。每个验证任务/基线或候选会有独立请求、输出、
总 Token 额度；优化调用不能借走 held-out 配额。建议先调用 `harness2_estimate_budget`，确认
任务数增加后每个 session 的额度仍够用。

主要报告字段：

- `comparison_valid`：所有预声明基线/候选配对都完成且没有基础设施错误。
- `task_results` / `family_success_lift`：逐任务及按用户声明家族汇总。
- `success_lift_95_ci`：以任务家族为 cluster、10,000 次确定性 bootstrap 的 95% 区间。
- `effect_conclusion`：包括 `improvement_supported`、`regression_supported`、
  `effect_inconclusive`、`no_detected_effect`、`insufficient_independent_holdout_families`、
  `no_candidate_aa_control_only`、`invalid_comparison`。
- `ledger`：provider 实际 usage、保守预留、完整性和可选 USD 估算。未提供准确单价时美元成本
  保持 unknown；Token 上限不是美元硬上限。
- `codex_worker_errors` / `invalidation_reasons`：Codex 流错误、已观察到的 request ID，以及
  对照失效的具体原因。日志没有 HTTP 状态码时会保持未知，不会从错误文本猜测。
- `provider_responses_missing_usage`：已发出请求但响应未提供可解析 usage 的数量；此时保留保守
  预留，账单会明确标为不完整。

`KEEP_BASE` 或空 diff 会触发默认 A/A 控制，但这只能给出可靠性信息，不能估计候选效果。少于
10 个独立任务家族、验证文件未隔离、任一配对缺失、超时、取消、预算或 provider 故障时，不能
称为有效提升。区间跨零表示未建立正向效果，不等同于证明零效果。绝不重试挑选有利样本、修改
裁判、伪造 usage 或篡改签名报告来“得到结论”。

## 5. 预览、应用与回滚

本教程只展示结果。应用前调用只读 `harness2_preview_apply`，检查完整 diff 和项目快照是否漂移。
只有保护测试的多家族区间支持正向提升、配对无回归且报告完整，系统才会给出确认令牌。之后也
必须由用户明确要求应用，并亲自在 OS 原生确认框中批准；模型不能用参数跳过确认。应用过程拒绝
覆盖发生变化的项目文件。

回滚必须由用户明确要求、再亲自在原生对话框确认。回滚会恢复原始字节和权限模式；若应用后文件
又被用户修改，则拒绝覆盖。无图形界面时可以实验和预览，但不能应用或回滚。

## 6. 运行记录与优化边界

记录默认保存在 `~/.codex/harness2-runs-v1/`，可用 `HARNESS2_STATE_DIR` 覆盖。报告、事件、
调用轨迹、项目快照、任务/裁判哈希、预算、diff 与部署日志都在项目目录之外并由本地密钥签名。

可优化面仅包括根 `AGENTS.md` 的追加内容、规范目录下的项目 Skills/新 helper、受限的
`shell`/`apply_patch` 工具开关，以及实际生效的 reasoning effort/timeout。不能修改代码、测试、
任意 MCP 命令、hooks、权限策略、全局配置、嵌套 `AGENTS.md` 或 Codex 审批设置。

附件里的 `evaluation/no-docker-testkit` 只做三个确定性裁判烟测和 dry-run 预算预览，不调用
模型，也不是 Harness² 效果测试。

## 既有真实运行记录

2026-10-22 至 2026-10-23 的旧版实际运行中，一次完整 `k=1` PPC（`R=4/P=3`）选中
`KEEP_BASE`，无候选差异；四组 A/A 后第五组出现 provider 启动错误，签名报告将对照判为无效；
另一次 PPC 遇到 HTTP 502。已报告 Token 分别为 1,823,845（usage 完整）和 764,227（usage
不完整），因缺少单价不报告美元成本，也没有应用差异。这些运行早于多任务隐藏裁判协议，不能
算作新版统计效果证据。

2026-10-23（上海时间）另以新版代码实际启动两次 `k=1` PPC，使用同一份预登记的 10 家族、
每项两次重复的隐藏验证计划。两次都在生成可选候选前失败：各有 67 个 provider 请求返回
HTTP 502，随后优化资源预留耗尽；签名报告均为无候选、`validation: null`、空 diff，没有
确认令牌，也没有应用任何文件。provider 没有返回 usage，不能将账本中的 `total_tokens: 0`
解释成免费；按保守请求预留分别计入 3,197,472 和 3,183,595 Token。另有一次独立的只读
连通性探测成功，usage 为 14,103 Token，但它不是 Harness² 实验。未提供准确单价，因此美元
成本未知。

这些新版运行**没有形成优化效果结论**：它们既不能证明有收益，也不能证明没有收益；只证明
当时 provider 的 502 故障令 PPC 无法产出候选，独立 held-out 验证没有启动。离线夹具、连通
探测和上游单元测试都不能替代这项缺失的实测。

2026-10-23 另一次真实 A6API + `gpt-6.1-sol` PPC 完成了候选生成并运行 5 组单任务留出对照，
基线 `1/5`、候选 `3/5`。报告仍为 `validation_inconclusive`：候选执行中出现 Codex JSONL
`stream disconnected before completion`，关联 request ID，但没有可确认的 HTTP 状态码；provider
HTTP 错误账本未记录这类流错误。另有候选轮次退出码异常，usage 不完整。表面 `+40` 个百分点
不是有效收益结论；单任务家族也不满足确认性效果评估。账本记录约 1,864,342 Token，usage
不完整，美元成本未知；没有应用任何差异。该观测促成本版补上 Codex worker 流错误审计、usage
缺失计数和逐项失效原因，并增加单次实验选择已配置 provider/model 的入口。

随后一次新版 `k=1` 顺序 PPC 确实执行了 Propose、Probe、Compose，并使用同一套 10 家族、
每项两次重复的隐藏验证计划；一次 Compose 断流在全新上下文恢复，一次 HTTP 502 provider
重试恢复。但最终候选 rollout 及其单次重试均遇到 HTTP 502，因而没有可选候选，holdout 未启动，
报告为 `optimization_inconclusive`。52 次请求中 48 次有 usage，共 493,501 Token；4 次缺 usage，
保守预留 777,393 Token。空 diff、无确认令牌，项目快照哈希未变。此为真实 PPC 基础设施失败，
不能解释为有效提升或无效果；美元成本未知。

当前源代码测试为插件 55 项、上游原版 5 项；另有累计报告校验测试 19 项通过。这些是实现与安全测试，不能替代真实项目 PPC/held-out
效果评测。更多审计见 [INTEGRATION_AUDIT.md](INTEGRATION_AUDIT.md)。

随后一轮完整记录了 40 项：28 项通过，后 12 项因 A6API 明确标注的
`upstream_unavailable` 打开熔断器后未能复位而成为基础设施失败。基线和候选各为 `14/20`，
对照无效；所选 harness 本身也没有差异，不能称为“证明无效果”。251 次请求中 239 次返回
usage，共 1,830,387 Token；缺 usage 的 12 次保留预留后，保守核算为 2,416,786 Token。
未生成应用令牌，项目快照哈希未变。这个故障促成了新的回归测试和熔断复位修复。

这里的“28”是通过数，不是停止条件：`10 个任务 × 2 次重复 × 2 个对照组 = 40 条记录`。
该轮 40 条均已归档，分为 `28 pass + 12 infrastructure_error`；后者不是模型任务失败，
也不是预算耗尽。读取进度时应分别展示完成数、通过数和基础设施错误数。只有完整且有效的
配对结果才可用于效果判断；不能删掉错误记录或只重放失败项来拼成有效对照。

可以另建一份**累计验收报告**，保留原轮 28 条通过，再引用固定 12 条基础设施故障记录的
补测结果，汇成同一张 40 行表。补测映射应在看到目标结果前冻结，核对项目快照、任务提示、
裁判、模型、预算和 harness 内容一致；每行标注来源轮次及原始报告、裁判记录哈希。只有
这 12 条也全部通过，才能标为“累计验收 40/40（原 28 + 补测 12）”；补测失败不能再挑选
其他成功记录替换。若发现并修复具体的基础设施缺陷，可在调用模型前另行冻结修复后补测计划，
对受影响任务重跑两组及全部原定重复；只引用预先固定的缺失行，完整保留其余行和先前失败。
原轮错误和所有轮次完整记录仍须保留。累计验收不是“单轮无故障 40/40”，
也不产生跨轮优化效果估计或应用令牌；它不会让原先 28 条通过记录作废。

另一个修正是上述顶层 `task` 的含义：此前误传优化元任务，worker 只能报告缺少实验工具，
PPC 因此没有获得实际函数修复的训练证据。新的完整实测改用独立的摄氏转华氏修复任务训练，
保留原 10 家族裁判及两次配对重复。该完整轮次现已结束，40 条记录中 39 条通过；剩余一条因
实际上报用量超过单次估算而被旧核算路径拒绝，不是任务裁判失败。旧路径预留 44,613 Token，
接口上报 133,752 Token，仍在该组 340,909 Token 配额内；历史核算现额外补记 89,139 Token，
不改写原签名报告。PPC 完整执行，但 Compose 保留原 harness，diff 为空。

修复核算后，对该任务预先冻结“两组各两次”的 4 条真实补测，保持相同模型、快照、提示、裁判、
harness 和每组配额。4 条全部通过，其中一次断流由全新隔离会话恢复。只引用预先指定的缺失行，
其余 3 条作为完整补测历史保留。最终生成一张 **累计验收 40/40（原 28 + 补测 12）** 的报告，
完整保存三轮 84 条记录。三轮已知 usage 为 4,680,315 Token，含缺失 usage 预留及历史补记的
保守核算为 6,102,910 Token；全部已归档实验账本累计核算为 30,362,576 Token，低于授权的
50,000,000。未提供费率，美元成本未知。

逐项来源与报告哈希见 [累计验收记录](RECOVERY_2026-10-23.md)。没有应用修改，没有应用令牌，
原测试项目快照哈希未变。接口修复或模型正常返回不是候选有收益的证据；这次确定恢复了累计可执行性，
但没有候选配置变化，不能宣称优化提升。该验证套件已复用，只能反映这组工程验证任务，
不能据此声称对所有真实项目都有收益。
