---
name: harness2-optimize
description: Run real pinned Google Harness2 Propose-Probe-Compose experiments on project AGENTS.md, canonical Skills, bounded tool controls and execution strategy. Use only for an explicitly requested harness experiment, not ordinary coding tasks.
---

# Harness² Optimize

Use the plugin's MCP tools, not a prompt-only reconstruction.

1. Call `harness2_inspect` with the exact absolute project root. Inspect existing
   tests and agree a trusted `check_argv` before a paid experiment. The root must
   be a specific project, not `/data` or another mixed collection of projects.
2. Call `harness2_run` with the task and check argument array. Start with `k=1`,
   two validation repeats, conservative request/output-token budgets. Preserve
   the user's exact configured model. Do not weaken the project or host policy
   when a prerequisite fails; report the failure.
   `max_total_tokens` includes provider input and output tokens (default 10M,
   maximum 50M). Validation sessions receive equal, nontransferable request,
   output and total-token envelopes before optimization starts. Unknown usage
   retains a conservative reservation. Inspect `resource_envelopes` in costs.
   An optional common `execution_timeout_sec` (30..600) overrides session
   timeout for every arm; disclose that timeout-strategy optimization is then
   not evaluated. It does not change filesystem/network/approval permissions.
   When the user requests full validation even if PPC keeps the base, set
   `validate_unchanged=true`. Such checks are a reliability control, not an
   improvement experiment; they never create an application token.
3. Poll `harness2_status` for actual progress. Report the upstream R/P ledger,
   baseline/candidate pass contrast, independent validation, exact changed files,
   and actual token usage. Unknown dollar costs stay unknown. Equal test passes
   are non-regression, not demonstrated improvement. Do not claim unrun checks.
4. Present the verified diff and confirmation token. Only when the human asks
   to apply, call `harness2_apply`. The native OS dialog is the authoritative
   final gate; do not click it using automation or bypass it.
5. Use `harness2_rollback` only when the user requests undo; it also requires
   the native dialog. Explain stale-file refusals instead of overriding them.

All candidate runs use the unmodified upstream PPC engine in OS-isolated
copies. Existing AGENTS/Skill bytes are preserved as an exact prefix. Only
canonical `.agents/skills` plus bounded real tool/effort/timeout options are
optimized. Arbitrary MCP commands, global settings, hooks, permission policies
and nested AGENTS.md are not modified. User execpolicy rules are preserved.
Do not hand-edit the original harness or the signed experiment state to get
around failed validation, drift checks or human confirmation.
