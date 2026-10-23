# Harness² for Codex (Unofficial Integration)

Uses the **unmodified Google Research core** pinned to
`1cfe254e8d25b15f37048c3730134686abe60335` (2026-10-14), not a prompt-only
imitation. File hashes are verified before each experiment. The adapter calls
upstream `recursion.run_parallel` / `run_sequential`, the upstream anonymous
selector, evidence builder, retry/fallback mechanism, and experience reflector.
Both modes enforce upstream `R=4k`, `P=3k`; selection, reflection, and additional
hold-out validation are separately accounted. Parallel mode composes from all
2k probes; sequential mode carries the preceding harness and evidence.

中文使用说明与当前环境限制见 [docs/USAGE.zh-CN.md](docs/USAGE.zh-CN.md)。

## Requirements and Installation

- Linux with working unprivileged user/mount/network namespaces and `bwrap`.
- Python 3.10 or newer. The adapter's vendored runtime has no third-party Python dependencies.
- Installed Codex CLI supporting plugins and its native Linux sandbox.
- Existing API-key-backed Codex Responses provider. Uses the active provider by
  default; a run may explicitly select another provider already configured in
  Codex and a model listed in its model catalog. Real credentials stay in the
  host-side broker; workers receive only revoked-after-use local proxy tokens.
  ChatGPT OAuth-only accounts fail closed.
- Tkinter and a graphical desktop for the final native confirmation dialog.

```bash
codex plugin marketplace add wzn1118/harness2-codex
codex plugin add harness2-codex@harness2-local
```

Start a new chat to load the plugin's Skill and MCP tools. This does not modify
the Codex executable or UI source. It adds native plugin discovery and MCP tools.

### Provider Selection

`harness2_inspect` and `harness2_run` accept optional `model_provider` and
`model` fields. The provider must already exist in the host `config.toml`; a
different model must be listed in its `model_catalog_json`. This selects a
provider only for that experiment and does not edit Codex configuration. For
example, after configuring an A6API Responses provider in Codex, a run can use
`model_provider: "A6API"` and `model: "gpt-6.1-sol"`.

To use a provider key held in a local file, set
`HARNESS2_PROVIDER_API_KEY_FILE` in the environment used to start Codex. The
path must be absolute; the file must be owned by the Codex user and have mode
`0600` or stricter. The plugin reads it in the host-side broker, never copies
the key into the project or worker home. The MCP manifest explicitly forwards
this path variable. Do not put key contents in tool arguments, project files,
or the Codex model catalog.

Chat example:

> 用 Harness² 优化这个项目，先做 k=1 的真实实验。任务是修复当前测试失败，
> 独立验证使用 `/usr/bin/python3 -m unittest discover -s tests`。

The chat must supply the **explicit absolute project path**, optimization task,
and either a legacy `check_argv` or a multi-task `validation_tasks` suite. For
effect claims, use at least 10 caller-declared task families with distinct,
protected verifier files. Verifier files are withheld from every model session
and restored only for the independent check. Two paired trials in alternating
order are required by default. A report includes per-task outcomes, a
deterministic family-cluster bootstrap 95% interval and a conclusion class.
Family independence is caller-declared and must be reviewed; the software cannot
infer domain-level relatedness. A candidate must pass every candidate check and
not regress any paired baseline result. Only a complete protected suite with a
positive interval can become eligible for application. A/A, KEEP_BASE, one-task
tests, incomplete comparisons and intervals crossing zero are never reported as
an optimization gain or issued an application token.

## Actual Editable Surfaces

- `AGENTS.md`: existing bytes retained as an exact prefix; additive edits only.
- `.agents/skills/<name>/SKILL.md`: existing prose preserved; new Skills and
  associated helper files supported. Existing helpers are immutable.
- `harness2-tools.json`: `shell` and `apply_patch` booleans are mapped into the
  real Codex child configuration. Tools cannot be enabled beyond the baseline
  envelope. Arbitrary MCP endpoints/commands, credentials, hooks and permission
  configuration are deliberately not editable.
- `harness2-execution.json`: `reasoning_effort` (`low`, `medium`, `high`) and
  `timeout_sec` (30..600) actually drive child execution, not inert prose.

Nested AGENTS.md, noncanonical skill directories, arbitrary `config.toml` / MCP
modifications and global settings are preserved, **not optimized**, in this
release. MCP/hook/project config auto-start is disabled only in worker copies.
Existing user execpolicy rules are copied to worker homes. Sandbox level is
capped at workspace-write and never elevated from read-only; approval policy
is retained. Codex's native sandbox runs inside a separate user/mount/network
namespace boundary. Namespace-local CAP_SETFCAP is retained only to allow its
nested uid mapping; no host capabilities are granted. No `dangerously-*` flags
or legacy-Landlock permission fallback are used.
Worker configuration, credentials and rule files are mounted read-only.
Named profiles, custom filesystem sandbox policies and unknown approval or
permission presets fail closed rather than being approximated.
Project/ancestor `.codex/config.toml`, inherited ancestor/root-override
instructions, additional host instruction files and system requirements also
require an explicit policy-import adapter and currently refuse to run. This
prevents a clean worker home from silently discarding project restrictions.

## Tools and Safety Gates

- `harness2_inspect`: read-only inventory and prerequisite check.
- `harness2_estimate_budget`: preallocates request/output/token ceilings without
  starting provider calls; it is not a dollar quote.
- `harness2_run`: starts a background experiment; no original project writes.
- `harness2_status`: progress, behavioral evidence, exact diff, provider/model,
  per-invocation errors and usage completeness.
- `harness2_get_report`: reads the integrity-checked signed report.
- `harness2_preview_apply`: checks eligibility and project drift without writes.
- `harness2_cancel`: cancels and kills isolated process groups.
- `harness2_apply`: requires reviewed run token **and a native human GUI click**.
  A model-supplied boolean/string cannot bypass the dialog. Full snapshot hash
  drift blocks application, including user edits made during confirmation.
- `harness2_rollback`: native confirmation, exact original bytes and modes,
  refuses to overwrite post-apply user changes. Journals permit recovery from
  interrupted application. Future experience entries are removed on rollback.

Runs live outside projects under `~/.codex/harness2-runs-v1/` (override with
`HARNESS2_STATE_DIR`). State changes are HMAC-bound. The snapshots, JSONL
trajectories, diffs, upstream stage ledger, paired check results, provider usage,
deployment journals and original bytes remain locally traceable. Files matching
secret names, symlinks, dependency caches and build outputs are excluded from
snapshots; embedded private-key/API-key signatures cause a refusal.
Worker-created symlinks, hardlinks and nonregular artifacts are rejected before
host-side evidence or harness readers run, preventing namespace-external link
targets from being read after an isolated process exits. Codex `error`,
`turn.failed` and `response.failed` events are preserved as sanitized worker
diagnostics with request IDs; they invalidate affected comparisons. The ledger
separates provider request count from responses with usage and explicitly counts
missing-usage responses. An absent HTTP status is not inferred from an error
message.

**No unsandboxed fallback.** No OS services, approvals, execpolicy rules or
global trust settings are relaxed by the plugin. Native GUI confirmation is
unavailable on headless hosts, so those hosts may experiment but cannot apply.

## Budgets and Cost Accounting

Each run has maximum provider-request, output-reservation and total-token
envelopes, enforced before dispatch (including concurrent requests). The
defaults are 160 requests, 655360 reserved output tokens and 10M total tokens;
the supported total-token maximum is 50M. For `T` validation tasks and `R`
repeats, resources are split into `2*T*R+4` units: one immutable unit per
validation session, one for reflection, and three for optimization. Remainders
stay with optimization. This prevents a late baseline from being starved by
earlier candidate or optimization calls. Per-request output reservations are capped
at 4096 and are not refunded. Total tokens reserve client request UTF-8 bytes
plus 8192 framing tokens and output before dispatch, then reconcile to actual
provider usage. Unknown usage keeps the conservative reservation. Unreported
provider-internal tokenization cannot be independently guaranteed by the
client; unexpected usage beyond reservation invalidates the execution.
The first provider request in a run is single-flight; after it succeeds, up to
8 requests may be in flight. Any provider HTTP error or transport failure opens
a per-run circuit breaker, so queued workers are rejected locally without
another upstream call or budget reservation. Codex worker retries are disabled
for the Responses provider. The audit ledger retains sanitized provider status,
host, request ID and numeric `Retry-After`; it never records response bodies or
credentials. The failed upstream request keeps its conservative reservation
because reported usage is unknown.
Optional `execution_timeout_sec` applies the same 30..600 second ceiling to
every session; when set, it deliberately disables evaluation of candidate
timeout changes, without changing any permission policy. Timeout and
cancellation kill entire worker process groups. Provider `usage` is collected
host-side even for child sessions. Missing usage is explicitly incomplete, not
zero. No guessed model prices: `cost_usd` stays `null` unless the caller supplies
`prices.input_per_million` and `prices.output_per_million`. Reported dollar cost
is then an estimate, not a billing invoice. Input tokens and context growth
are included in the total-token envelope, but this is **not a hard dollar-spend
cap**, particularly when usage or provider pricing is unavailable.
The vendored upstream method/selection records contain their original `usd`
placeholder; only the separate provider `ledger` is billing evidence. Do not
interpret an upstream `usd: 0` as a zero-cost experiment.

Experiments send selected source files and traces to the already configured
model provider. A user should only include projects they are authorized to send.
Original model credentials, other projects, state signing keys and unrestricted
network are not exposed to the workers. This is experimental code, not a
formal security certification or an official Google/OpenAI integration.

## Validation

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=vendor/harness2-upstream python3 -m unittest discover -s vendor/harness2-upstream/tests -v
```

See `vendor/harness2-upstream/PROVENANCE.json`, upstream `LICENSE`, and
`docs/INTEGRATION_AUDIT.md` for source pins, extension evidence and limitations.

The deterministic no-Docker judge smoke test is documented in
`evaluation/no-docker-testkit/README.md`. It makes no model calls and is not an
optimization-effect benchmark.
