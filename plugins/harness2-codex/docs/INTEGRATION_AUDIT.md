# Source, Integration, and Experiment Audit

Audit updated: 2026-10-23 (Asia/Shanghai). This is an unofficial integration,
not a Google Research or OpenAI product.

## Upstream and Codex Integration

- Google Research Harness² is pinned to commit
  `1cfe254e8d25b15f37048c3730134686abe60335` (2026-10-14). Source hashes and Git
  blob identifiers are recorded in `vendor/harness2-upstream/PROVENANCE.json`;
  runtime checks the vendored SHA-256 hashes before experiments.
- The vendored upstream core and Apache-2.0 license are unchanged. The adapter
  calls upstream parallel/sequential recursion, retry/fallback, evidence,
  anonymous candidate selection, reflection and experience-library code.
  Harness² stages are not simulated with chat instructions.
- Codex integration uses a native plugin manifest, a packaged Skill, and a
  stdio MCP server. Nine tools cover inspect, budget estimate, run, status,
  signed report, read-only apply preview, cancel, apply and rollback. Relative
  MCP paths resolve inside the installed plugin directory.
- The installed CLI and native plugin/marketplace commands were exercised on
  Codex CLI 0.153.4 on 2026-10-22. Plugin manifest validation, Skill validation,
  marketplace discovery, MCP initialization, all nine tool registrations and a
  read-only runtime inspection passed in that environment. This is an observed
  compatibility point, not a claim that all Codex versions are supported.

## Safety and Supported Scope

- Experiments run in isolated Linux user, mount and network namespaces, with
  Codex's native sandbox retained inside the worker. There is no unsandboxed
  fallback, permission escalation, or change to host approval configuration.
- The project is only read during experiments. Editable candidates are limited
  to additive root `AGENTS.md` content, canonical project Skills and helper files,
  bounded tool booleans, and bounded reasoning/timeout options. Existing text is
  preserved as a prefix; scripts, arbitrary MCP commands, hooks, permission
  policy, global settings, nested `AGENTS.md`, source code and tests are not
  deployment surfaces.
- Project policy that cannot be faithfully inherited, unsupported Codex
  permission profiles, missing namespace prerequisites and unsupported
  providers fail closed. The current broker requires an API-key-backed
  Responses provider; ChatGPT OAuth-only authentication is unsupported. Runs
  may select only a provider already configured in Codex; model overrides must
  be present in the configured model catalog. An optional key file is accepted
  only when absolute, regular, owned by the Codex user and mode 0600 or stricter.
- Confirmatory validation accepts caller-defined task prompts and verifier
  commands. Declared verifier files are withheld from every model worker and
  restored only in the separate checker workspace. At least 10 caller-declared
  task families are required for a confirmatory effect interval; the software
  cannot independently establish that family labels are truly unrelated.
- The report computes a deterministic 10,000-resample cluster bootstrap over
  family-level task success lifts. Deployment requires a complete protected
  suite, a 95% interval wholly above zero, all candidate checks passing, no
  paired regression, an unchanged project snapshot, a signed report, and native
  user confirmation. A/A or KEEP_BASE outcomes never produce an effect estimate
  or application token. Provider errors, budget exhaustion, cancellation,
  timeouts, stale snapshots and signed audit invalidations prevent deployment.
- Apply and rollback require a reviewed run and a native human confirmation
  dialog. The test host was headless, so a production GUI confirmation was not
  exercised. Test-only injected callbacks verify transaction logic but are not
  exposed through MCP and do not stand in for human confirmation.
- Experiment records are stored outside the project. Project source and model
  traces are sent to the configured provider; users must have authority to send
  that material. Token reservations are enforced, but unavailable provider usage
  cannot be independently verified and there is no hard dollar cap. USD cost is
  not estimated without caller-supplied prices.
- Codex worker `error`, `turn.failed` and `response.failed` JSONL events are
  retained as sanitized diagnostics and invalidate affected comparisons.
  Request IDs are extracted without inventing HTTP statuses. Provider request
  counts and responses with usage are reported separately; missing usage is
  explicit.

## Verification and Actual Runs

Validated on 2026-10-22/23 (the source tests were rerun after the latest worker
retry changes):

- Plugin safety/integration tests: 55 passed, including A6API profile/key-file
  selection, stream-disconnect diagnostics, family-bootstrap
  classification, budget estimates, hidden-verifier isolation and read-only
  deployment preview. All 10 synthetic effect-benchmark verifiers reject the
  deliberately buggy baseline; this validates fixtures, not optimizer quality.
- Unmodified upstream workflow tests: 5 passed.
- Private cumulative-report verification tests: 19 passed, covering fixed-row
  provenance, source/checker tampering, incomplete/duplicate rows, protocol
  drift, retention of earlier passes/failures and accounting correction.
- Plugin manifest and Skill validators passed; the native MCP smoke test
  initialized the server and listed all nine tools. The no-Docker testkit
  preflight, three deterministic hidden-judge fixtures and dry-run planner pass
  without Docker, credentials or model calls.
- A real, complete `k=1` parallel PPC run performed upstream `R=4/P=3`, probes,
  composition and selection. The upstream selector returned `KEEP_BASE`; there
  was no diff, no independent comparison was needed for adoption, and no
  application token was issued. Provider usage was complete at 1,823,845 tokens.
- A diagnostic A/A continuation tested an unchanged surface. Four full pairs
  completed, but the fifth candidate failed before a provider request. The
  signed report invalidated the overall comparison; this is not five-pair
  validation and is not evidence of improvement.
- A later real PPC execution encountered repeated provider HTTP 502 failures.
  No usable candidate was selected and independent validation did not run. The
  reported 764,227 tokens are incomplete usage, not a completed-run total.
- On 2026-10-23 (Asia/Shanghai), two additional real `k=1` PPC runs used the
  preregistered 10-family, two-repeat protected suite. Each made 67 provider
  requests, all answered HTTP 502; subsequent local retries exhausted the
  optimization envelope. Both integrity-checked reports had no candidate,
  `validation: null`, empty diff and no confirmation token. Provider usage was
  unavailable, so conservative reservations were 3,197,472 and 3,183,595
  tokens; these are not provider-reported usage or USD charges. A separate
  read-only connectivity probe succeeded with 14,103 reported tokens, but is
  not Harness² evidence. Combined budget accounting for these two runs and the
  probe was 6,395,170 tokens. No files were applied.
- A later real A6API + `gpt-6.1-sol` run completed PPC candidate selection and
  five single-task hold-out pairs. Raw success was 1/5 baseline versus 3/5
  candidate, but the signed report correctly classified the comparison as
  `validation_inconclusive`, not an improvement. The retained Codex JSONL shows
  `stream disconnected before completion` with a request ID; no HTTP status can
  be verified from that event. The old provider-error ledger missed this worker
  stream error, and usage was incomplete (1,864,342 tokens recorded; USD
  unknown). The source project was unchanged and no candidate was applied.
  This finding led to the worker-error audit, missing-usage counters and
  explicit per-row invalidation reasons now covered by tests.
- After that upgrade, a real `k=1` sequential PPC ran against the bundled
  10-family, two-repeat effect benchmark using the active `gpt-6.1-sol` route.
  The first provider request returned HTTP 502; the circuit breaker prevented
  further provider calls. No candidate was produced, `validation` was null,
  the diff was empty and the benchmark project remained unchanged. The signed
  report retained the provider failure and worker request ID. Provider usage
  was unavailable; the conservative reservation was 41,404 tokens, not
  provider-reported usage, and USD cost is unknown. The active Codex config did
  not contain an A6API provider, so this run was not an A6API run. This verifies
  failure accounting only; it provides no optimization-effect evidence.
- On 2026-10-23, a real `k=1` sequential run used the recovered user-provided
  A6API credential with `gpt-6.1-sol` and the same 10-family, two-repeat suite.
  PPC reached Compose, but one HTTP-200 Responses stream ended with an upstream
  “service temporarily unavailable” error. The signed report classified the
  run as `optimization_inconclusive`: no candidate was selected, no holdout
  validation ran, and the project diff was empty. Of 38 provider requests, 37
  returned usage totaling 379,458 tokens; one response had no usage, so
  conservative budget accounting was 424,688 tokens. USD cost is unknown and
  no project files were applied. This is a transport failure record, not an
  optimization-effect conclusion. Follow-up code passes the selected model
  catalog read-only to workers, allows one budgeted clean-session retry for
  this stream-disconnect class, and retries HTTP 502/503/504 once with a
  separate budget reservation.
- A subsequent real A6API + `gpt-6.1-sol` run used the same multi-family PPC
  request and read-only model-catalog snapshot. It ended
  `optimization_inconclusive` before candidate selection: Compose timed out
  after 600 seconds and three rollout workers reported stream disconnects.
  The provider broker observed no HTTP errors, but these Codex worker failures
  invalidated PPC; no holdout validation ran, no candidate/diff was produced,
  and no project files were applied. The provider reported usage for 29 of 32
  requests (335,091 total tokens); three responses lacked usage, so conservative
  budget accounting was 495,352 tokens. USD cost is unknown. This is evidence
  of a timeout/stream failure, not evidence for or against optimization.
  Follow-up code retries an explicit worker stream disconnect or execution
  timeout once in a fresh session/workspace for PPC, rollout, and holdout calls;
  each retry is charged to its original PPC or holdout-arm allocation and both
  attempts remain auditable. The updated retry path has not yet had a complete
  confirmatory live run.
- Two more real `k=1` A6API + `gpt-6.1-sol` multi-family runs stopped before
  candidate selection. The first received provider HTTP 403 on its initial
  Responses request: usage was absent, the conservative reservation was
  45,423 tokens, and no holdout or diff was produced. In the next run the first
  request returned HTTP 200 but its Responses stream contained an upstream
  “service temporarily unavailable” error; the single fresh-workspace retry
  received HTTP 403 and opened the circuit. Neither response reported usage;
  conservative budget accounting was 90,846 tokens. Both signed reports have
  empty diffs, no candidate and no confirmation token. Separate minimal
  connectivity checks with the same key/model succeeded, including one isolated
  plugin broker call, but these checks are not PPC evidence and do not explain
  the full benchmark request's 403. No project files were applied.
- The local MCP broker now distinguishes budget rejection and local failures
  from upstream HTTP errors (429/400/500 rather than a fabricated 502). Provider
  HTTP error details are retained only after bounded credential redaction, so
  the next failed live request can be diagnosed without logging response
  bodies or API keys.
- A real `k=1` sequential PPC was then run with the updated retry/backoff build
  and the same 10-family, two-repeat suite, capped at 12M tokens. Propose,
  Probe, and Compose executed. One Compose stream disconnect recovered in a
  fresh context; one provider 502 recovered on retry. The final candidate
  rollout then encountered HTTP 502 and its single retry also failed, so PPC
  produced no selectable candidate and did not start held-out validation. The
  signed report is `optimization_inconclusive`, has no diff or confirmation
  token, and the benchmark snapshot hash is unchanged. Of 52 provider
  requests, 48 returned usage totaling 493,501 tokens; four lacked usage and
  conservative budget accounting was 777,393 tokens. Two provider retry
  episodes were recorded, one recovered; one Codex clean-context retry
  recovered. USD cost is unknown. This is a completed real PPC execution with
  infrastructure failure, not an optimization-effect conclusion. The new
  provider-error detail capture did not yield a diagnostic for the upstream
  502 responses.
- Based on this evidence, the source now permits up to two provider retries for
  HTTP 502/503/504 and up to two clean worker retries for explicit stream
  disconnects/timeouts, with increasing backoff and each attempt charged to
  the original allocation. `IncompleteRead` remains limited to one retry;
  non-transient HTTP errors still fail closed. A fresh worker retry can reset
  only a transient 502/503/504 provider circuit; each reset is audited. This
  recovery path has passed the plugin regression suite and is being checked by
  another live PPC/holdout run.
- A real `k=1` sequential A6API + `gpt-6.1-sol` run completed PPC and selected
  a candidate, then executed all 40 rows of the same 10-family, two-repeat
  hidden holdout. A6API returned sustained HTTP 503 overload responses during
  the pagination baseline; the run-wide provider circuit then blocked later
  sessions. The report counted 21 passing rows and 19 infrastructure failures,
  so it correctly marked the paired comparison invalid. The raw pass counts
  were baseline 10/20 and candidate 11/20 (one pagination candidate pass); this
  difference is not an effect estimate because the comparison was incomplete.
  Provider usage was 1,446,850 tokens across 188 of 197 requests, with nine
  missing usage responses; conservative budget accounting was 1,871,232
  tokens. The signed report has an empty project diff and no confirmation
  token; no project files were applied. USD cost is unknown. This establishes
  neither improvement nor no effect and exposed the need to recover a transient
  per-run circuit between clean worker retries.
- The first live check of that recovery build then received HTTP 403 responses
  from A6API whose bounded error body explicitly used code `upstream_unavailable`
  and described the service as temporarily unavailable. The implementation
  correctly treated ordinary 403s as permanent, so it did not recognize this
  provider-specific transient signal. PPC stopped before selecting a candidate;
  there was no holdout or diff. Of 13 requests, 11 reported usage totaling
  107,224 tokens; two lacked usage, and conservative budget accounting was
  274,011 tokens. USD cost is unknown. The source now retries only this exact
  structured error code in addition to HTTP 502/503/504; ordinary 403 responses
  remain non-retryable. A regression test covers both the A6API mapping and
  permanent errors. This run did not exercise that provider-specific error code.
- A subsequent live check used the same suite with a 10M total-token ceiling.
  One rollout stream disconnect recovered in a fresh workspace, and A6API
  HTTP 503 responses were each retried twice. PPC did not produce a candidate:
  the three optimization allocations totaled 681,848 tokens and were exhausted
  while handling sustained `wallet_admission_saturated` and
  `system_cpu_overloaded` responses. The resulting local budget rejection
  prevented the clean-session retry from starting; it was not an upstream 429.
  The run used 26 provider requests, of which 22 reported usage totaling
  281,140 tokens; four lacked usage and conservative budget accounting was
  661,929 tokens. No holdout ran, the diff was empty and no confirmation token
  was issued. USD cost is unknown. This is an inconclusive infrastructure and
  budget-allocation run, not evidence for or against Harness² effect. The next
  live attempt raises the ceiling to 15M, preserving the per-arm holdout
  reservations while giving optimization enough room for its retry envelope.
- That 15M-ceiling run completed PPC and all 40 holdout records. The first 28
  records passed; an exhausted A6API HTTP 403 `upstream_unavailable` then opened
  a circuit that blocked the last 12 records. Both arms passed 14/20. The signed
  comparison is invalid, not a zero-effect finding; the selected harness was
  unchanged, so there is no candidate effect to estimate in any case. There
  were 251 provider requests, 239 responses with usage totaling 1,830,387 tokens,
  and 12 missing-usage responses. Conservative accounting was 2,416,786 tokens.
  Six of eight broker retries recovered, including an earlier coded HTTP 403;
  three clean worker retries recovered. The final 24 worker retry attempts
  could not recover because their circuit-reset condition recognized the HTTP
  transient reason but omitted the explicit `upstream_unavailable` reason.
  No circuit reset was recorded. This was an adapter defect, not evidence that
  the underlying task was unsolvable or that Harness2 has no effect. The source
  now routes both recognized reasons through the same restricted, audited reset;
  a new regression failed before the fix and passed after it. Ordinary permanent
  errors still cannot reset the circuit.
- Reviewing PPC's actual notes also exposed a task-design error: the previous
  top-level `task` requested harness optimization itself. Rollout workers are
  forbidden to change the harness and cannot run a nested experiment. This
  yielded capability-blocker evidence rather than representative function-repair
  evidence. The Skill and tutorial now explicitly distinguish the concrete
  rollout task from the upstream proposer/composer's harness-editing mission.
  A fresh full run used Celsius-to-Fahrenheit repair as its training task and
  keeps the same ten-family, two-repeat protected holdout suite. It does not
  replay only failed rows, edit judges, weaken baseline settings, or force a
  candidate. Its request and source hashes are frozen before results, with a
  separate signed client preregistration. The new 15M ceiling plus the recorded
  historical conservative accounting is below the authorized 50M token budget.
  The holdout suite has been reused for engineering verification; it is not a
  previously unseen sample supporting broad project-level generalization.
- That full run completed real PPC with `R=4/P=3/k=1`, no unrecovered PPC
  infrastructure errors, and unchanged-harness selection. It archived 40
  holdout records: 39 passed and one infrastructure error. The coded transient
  circuit reset recovered during the live run. The remaining error was an
  accounting defect: provider usage of 133,752 tokens exceeded the request's
  44,613 estimate but not the arm's 340,909 allocation. The old code rejected
  it and undercharged 89,139 tokens. The signed source report remains intact;
  the cumulative report explicitly adds that accounting correction. Known
  usage was 2,650,617 tokens across 323 of 340 requests; conservative accounting
  was 3,353,061 before, or 3,442,200 after, the correction. The paired comparison
  remains invalid. No candidate change or deployment token was produced.
- The source now charges above-estimate usage in full and continues only
  inside the original arm allocation. An actual allocation overrun is also
  charged in full but blocks further spending; scopes never share quota. New
  independent sessions may reset recognized transient circuits rather than
  inheriting an unrelated task's outage. Same-session retry limits and ordinary
  authentication/quota circuits remain enforced. Regression tests exercise
  success, above-estimate accounting, actual exhaustion and persistent audit
  records. The subsequent live supplement used this exact runtime build, but
  did not encounter another above-estimate usage or circuit-reset event; those
  new branches therefore have regression rather than live branch coverage.
- A separately frozen real supplement revalidated both arms, twice each, of
  the affected filename task with the original per-arm budgets, snapshot,
  model, prompts, harness and protected verifier. All four rows passed, with
  one worker stream disconnect recovered in a fresh isolated workspace. Only
  the one preregistered missing row was eligible for cumulative replacement;
  the other three rows remain in full history. Usage was 199,311 tokens across
  29 of 30 requests; conservative accounting was 243,924 tokens. No new PPC
  was simulated or claimed for this bounded revalidation.
- The integrity-checked cumulative acceptance is now **40/40: original 28 plus
  fixed 12 recovered rows**, retaining all 84 source records and the prior
  failures. This is operational acceptance across runs, not a fault-free
  single run, a pooled effect estimate or an adoption decision. The three
  sources total 4,680,315 known tokens and 6,102,910 conservatively charged
  tokens including the correction. All archived experiment ledgers together
  account for 30,362,576 tokens, below the authorized 50M ceiling. Source
  hashes and a public row projection are in `RECOVERY_2026-10-23.md`.
- No experiment diff was applied. USD costs are unknown because provider rates
  were not supplied. Ordinary test success and partial A/A observations do not
  establish Harness² improvement on a project.
- The older successful/partial model runs used the prior single-task protocol;
  several multi-family runs failed before candidate selection on provider
  HTTP/stream errors, worker timeouts, or preallocated optimization exhaustion.
  The latest full unchanged-harness A/A run completed PPC but had an invalid
  paired comparison. Its missing operational row has now been recovered by
  the separately frozen supplement, without changing that comparison's status.
  The A6API single-task run had an upstream stream failure and an invalid paired
  comparison. There is therefore no valid measured effect
  conclusion: neither a positive effect nor a zero/no-effect result has been
  established. Real PPC completion and cumulative execution recovery have
  now been observed; neither establishes a benefit from a changed harness.

These outcomes are intentionally reported as no valid measured improvement.
Run-specific event logs, absolute project paths, identifiers and private
experiment directories are not included in this public repository.

## Upstream Reference

Google Research Harness²: https://github.com/google-research/harness2
