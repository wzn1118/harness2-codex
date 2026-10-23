---
name: harness2-optimize
description: Run real pinned Google Harness2 Propose-Probe-Compose experiments on project AGENTS.md, canonical Skills, bounded tool controls and execution strategy. Use only for an explicitly requested harness experiment, not ordinary coding tasks.
---

# Harness² Optimize

Use the plugin's MCP tools, not a prompt-only reconstruction.

1. Call `harness2_inspect` with the exact absolute project root. Review the
   project and its trusted tests. Never choose a mixed parent such as `/data`.
2. Before provider calls, agree with the user on the optimization task, held-out
   prompts, verifier commands and budget. For an effect claim, prepare at least
   10 caller-declared task families, each with its own protected verifier file.
   Check that families are not paraphrases or from the same underlying issue.
   Call `harness2_estimate_budget` with task count, repeats and resource ceilings;
   explain that it estimates tokens/requests, not dollars. Obtain clear user
   approval of the spend before `harness2_run`.
   The top-level `task` is a concrete representative project task for the PPC
   rollout worker, such as repairing a named function under its documented
   contract. It is not an instruction to run Harness2 or to edit harness files:
   the upstream proposer/composer alone edit the candidate harness. Review this
   distinction explicitly before spending. A recursive harness-optimization
   request gives the rollout worker an impossible task and unusable evidence.
3. Start with `k=1` and two validation repeats. Preserve the user's exact
   configured model unless the user explicitly selects a `model_provider` and
   `model` from the host Codex config/model catalog. Preflight that pair with
   `harness2_inspect`; never change the global Codex config or silently fall
   back to another provider. Do not weaken project/host policies if a
   prerequisite fails.
   Supply `validation_tasks` as objects with `task_id`, `family`, `task`,
   `check_argv` (argument array, never shell text), and `protected_paths`.
   The protected files are hidden from every model session and restored only in
   the isolated judge workspace. Confirm task prompts are distinct from the PPC
   optimization task. Validation resources are preallocated per task/arm and
   cannot be borrowed. `max_total_tokens` includes input and output (default
   10M, maximum 50M); missing usage retains its conservative reservation.
4. Poll `harness2_status`, then call `harness2_get_report` for the signed final
   report. Report the real upstream R/P ledger, per-task and per-family results,
   bootstrap interval, conclusion class, exact diff, project unchanged status,
   and actual usage. Caller-declared family independence is an assumption, not
   software-verified fact. Unknown USD cost stays unknown. Never turn A/A,
   KEEP_BASE, incomplete data, a passing smoke test or an interval containing
   zero into a positive improvement claim.
   Inspect `codex_worker_errors`, `provider_responses_missing_usage` and
   `validation.invalidation_reasons`; preserve request IDs and do not infer an
   HTTP status that the provider response did not report.
   Transient provider circuits may be probed by a fresh isolated retry or the
   next independent session, within the existing retry and resource caps.
   Above-estimate reported usage is charged in full and continues only inside
   the same arm allocation. Do not bypass authentication, quota, permission
   or actual budget failures. Distinguish completed records, passes and
   infrastructure errors instead of treating a pass count as a stop condition.
5. Before discussing application, call `harness2_preview_apply`. Application is
   eligible only after a valid protected multi-family suite supports positive
   lift and no paired regression. Show the diff and evidence. Only if the human
   then asks to apply call `harness2_apply`; the native OS dialog is the final
   gate. Do not automate or bypass it.
6. Use `harness2_rollback` only when the user requests undo; it also requires
   the native dialog. Explain stale-file refusals instead of overriding them.

All candidate runs use the unmodified upstream PPC engine in OS-isolated
copies. Existing AGENTS/Skill bytes are preserved as an exact prefix. Only
canonical `.agents/skills` plus bounded real tool/effort/timeout options are
optimized. Arbitrary MCP commands, global settings, hooks, permission policies
and nested AGENTS.md are not modified. User execpolicy rules are preserved.
Do not hand-edit the original harness or the signed experiment state to get
around failed validation, drift checks or human confirmation.

If PPC returns KEEP_BASE, the default A/A control may characterize reliability,
but there is no candidate-effect estimate. If the comparison is incomplete,
report it as invalid and do not present it as a valid optimization result. The
system cannot guarantee that a model run will produce a candidate or a positive
effect; never force one by changing the judge, retrying selectively, or editing
the signed record.

If the user requests cumulative operational acceptance, keep it separate from
paired effect estimation. Freeze missing-row mappings before observing their
recovery outcomes; retain original passes and every failure. Infrastructure
fix revalidation must keep the frozen model, project, prompts, harness, judge
and per-arm budgets, and cover both arms and original repeats of an affected
task. Label a merged result as cumulative, not single-run; it cannot repair an
invalid effect comparison or authorize deployment.
