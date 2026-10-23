# HarnessPilot

HarnessPilot exposes the complete Harness² experiment workflow to Codex through an MCP server and a Codex skill. It provides project scanning, frozen experiment plans, budget estimates, approved baseline and recursive runs, independent validation, reports, and review-gated apply or rollback.

## Install

Requirements: Windows Codex Desktop with its managed CLI, WSL2 Ubuntu with systemd and cgroup v2 delegation, Python 3.12, bubblewrap user namespaces, Node.js, and an API-key-backed Responses provider in the selected Codex home.

```powershell
./scripts/install.ps1 -CodexHome $env:CODEX_HOME
```

The installer installs the same Codex CLI version into WSL, runs the MCP service in a delegated systemd scope as an unprivileged account, and registers the plugin with the matching Desktop-managed CLI. Each experiment receives a cgroup v2 CPU, memory, and process limit alongside bubblewrap isolation. It reads the selected `config.toml` and `auth.json`; it does not copy or rewrite those files. An existing local HarnessPilot plugin source is preserved as a timestamped backup before replacement.

Installation and doctor checks send no provider requests. The skill requires an explicit budget review and approval before a model-backed experiment. See [installation and operations](docs/INSTALLATION.md) and the [security boundaries](docs/SECURITY.md).

Run the deterministic security and upstream regression suites in Linux with `bash scripts/test.sh`. These tests make no model requests.

## Harness² source

In this repository, the runtime reuses the pinned, licensed source at `plugins/harness2-codex/vendor/harness2-upstream`. The installer also accepts a standalone checkout containing an `upstream/` directory. The recursive engine and its original tests are preserved without modification.

The bundled smoke task answers are public, including cases labeled `sealed`; they are not confidential holdout data. Results from this suite are engineering evidence, not a claim of general model improvement. Token budgets are not a USD spending cap.

HarnessPilot is an independent community integration and is not an official Google or OpenAI product.
