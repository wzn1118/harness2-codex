# A6API Validation Record

Validation date: 2026-10-24 (Asia/Shanghai)

## Runtime

- Codex CLI in WSL: `0.153.2`
- Python: `3.12.3`
- bubblewrap: `0.9.0`
- Node.js: `v22.23.4`
- Wire protocol: Responses
- Endpoint host: `api.a6api.com`
- Model and reasoning settings: read from the selected Codex `config.toml`
- Authentication source: the selected Codex `auth.json`; credential values were withheld

The doctor check reported `sandbox_ready=true`, `sandbox_isolated=true`, and only the
`lo` interface inside the model sandbox. The protected Codex files were read-only during
installation and validation.

## MCP And Regression Checks

- MCP tools exposed: 13
- HarnessPilot regression tests: 17 passed
- Pinned upstream Harness2 tests: 7 passed
- Project scan: 141 files, 4 editable files
- Task-suite SHA-256: `67b57f7a0cac4695ec41e6f1ee46622d641847e9d8619617ded83a5bc62802ec`

## Provider Probe

A read-only `GET /v1/models` probe against `https://api.a6api.com/v1` returned HTTP
`401 invalid_api_key`. No credential value was written to this repository or included in
logs.

## Baseline Attempt

The approved baseline plan reserved 12 estimated requests and 589,824 estimated tokens.
The run completed with 12 smoke tasks, 0 passed tasks, 0 provider requests, and 0
charged tokens. The selected repository path is the HarnessPilot source tree, which does
not contain the smoke task deliverables (`solution.py`, `solution.js`, or `solution.rs`);
the run therefore produced an execution-environment result, not a model-quality result.

After that run, the sandbox was corrected to map an external Node.js installation target
through bubblewrap. The new Node isolation regression passed. A successful model-backed
Harness2 result still requires a valid A6API key in the existing Codex authentication
file.
