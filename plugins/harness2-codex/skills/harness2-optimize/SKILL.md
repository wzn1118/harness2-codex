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
3. Start with `k=1` and two validation repeats. Preserve the user's exact
   configured model. Do not weaken project/host policies if a prerequisite fails.
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
