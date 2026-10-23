---
name: harnesspilot
description: Use the installed HarnessPilot MCP tools to inspect a project, estimate and explicitly start a Harness² baseline or recursive optimization, review evidence, and prepare approved apply or rollback operations.
---

# HarnessPilot

Use the MCP tools for all project operations. Do not edit project files directly as part of an experiment.

## Inspect

1. Resolve the user's active project to an absolute Windows or WSL path.
2. Call `hp_scan_project` with that path. Report the snapshot ID, file counts, active model and reasoning effort, and the pinned task-suite hash. Do not reveal authentication values.
3. Call `hp_list_tasks` for the requested split. The public smoke suite includes demo answers in its repository source, so its `sealed` label is not a confidentiality boundary or a publishable holdout benchmark.

## Run Experiments

1. Explain that current Codex config and auth are used, model requests send snapshot context to the configured provider, and HarnessPilot has no verified USD price cap.
2. Call `hp_estimate_budget` for `baseline` first. It sends no model requests. Present request/token limits and the estimate. Ask the user to explicitly approve the estimated provider usage.
3. Only after approval, call `hp_start_experiment` with the exact plan ID, plan hash, and one-time approval token.
4. Poll `hp_get_status`. Use `hp_cancel` only when asked. Use `hp_resume` for an interrupted or failed job after checking its error and budget.
5. Once the baseline completes, estimate and start `optimize`. HarnessPilot runs the pinned Harness² `agg` and `seq` modes, keeps separate per-mode improvement libraries, and selects only on validation data.
6. Present `hp_get_report` with the paired task/family counts, confidence interval, regressions, token usage, and limitations. A synthetic suite result is not a claim of general improvement.
7. Sealed evaluation requires a separately estimated and approved sealed plan. Call `hp_probe` only after that approval.

## Apply and Roll Back

1. Call `hp_preview_apply` and show the complete diff, changed-file counts, and validation evidence.
2. Ask the user to confirm the exact candidate and file diff.
3. Only after confirmation, call `hp_apply_candidate` with the preview plan ID, plan hash, and one-time token.
4. Verify the resulting project state. For rollback, call `hp_preview_rollback`, show its diff, obtain confirmation, then call `hp_rollback` with that rollback plan's exact values.

Never place credentials, auth files, private task cases, or raw secret values into prompts, reports, logs, or Git commits. Do not claim that token limits enforce a USD maximum. Do not infer a measured improvement from a proposal or a successful smoke test.
