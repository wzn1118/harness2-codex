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
  Responses provider; ChatGPT OAuth-only authentication is unsupported.
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

## Verification and Actual Runs

Validated on 2026-10-22/23:

- Plugin safety/integration tests: 32 passed, including family-bootstrap
  classification, budget estimates, hidden-verifier isolation and read-only
  deployment preview. All 10 synthetic effect-benchmark verifiers reject the
  deliberately buggy baseline; this validates fixtures, not optimizer quality.
- Unmodified upstream workflow tests: 5 passed.
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
- No experiment diff was applied. USD costs are unknown because provider rates
  were not supplied. Ordinary test success and partial A/A observations do not
  establish Harness² improvement on a project.
- The older successful/partial model runs above used the prior single-task
  protocol. The two newer runs used the protected multi-family protocol but
  failed before candidate selection and held-out evaluation. There is therefore
  no valid measured effect conclusion under the upgraded evaluator: neither a
  positive effect nor a zero/no-effect result has been established. Provider
  failures are now explicitly classified as optimization-inconclusive by the
  current source.

These outcomes are intentionally reported as no valid measured improvement.
Run-specific event logs, absolute project paths, identifiers and private
experiment directories are not included in this public repository.

## Upstream Reference

Google Research Harness²: https://github.com/google-research/harness2
