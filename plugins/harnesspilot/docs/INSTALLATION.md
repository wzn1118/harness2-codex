# Installation and Operations

## Install on Windows with WSL2

Run from PowerShell and point the installer at the Codex home already used by Codex Desktop:

```powershell
./scripts/install.ps1 -CodexHome $env:CODEX_HOME
```

The installer selects the newest `official-*` CLI package under that Codex home, reads its version, and installs the matching `@openai/codex` release in Ubuntu. Pass `-CodexCliPath` when the managed executable is stored elsewhere. The WSL installer provisions Python 3.12 dependencies, bubblewrap, Node.js, Rust, a dedicated `harnesspilot` account, and the pinned Harness² package. WSL systemd and cgroup v2 delegation are required.

The plugin entry is installed through the personal marketplace at `~/.agents/plugins/marketplace.json`. Its local MCP configuration is generated from `-CodexHome`; no machine-specific path is committed to this repository. An earlier plugin source at the marketplace path is moved to a timestamped sibling backup before the new source is installed.

The MCP command enters a transient systemd service with `Delegate=yes` and aggregate CPU, memory, and task limits, then runs under the dedicated account. Each experiment creates a child cgroup with its own CPU, memory, process, and file-size limits. The installer reads the existing Codex `config.toml` and `auth.json` from WSL, verifies that the service account can read them, and stores runtime state in `/var/lib/harnesspilot/state`, owned by that account. It verifies that effective model/provider settings and the `auth.json` hash remain unchanged after plugin registration; Codex records the enabled plugin in its own configuration. The MCP process keeps provider credentials in memory; its isolated Codex child receives a temporary local broker token.

Set `HARNESSPILOT_API_BASE_URL` in the MCP process environment to override only the endpoint used by HarnessPilot. The override is validated and included in the model fingerprint. It does not write either Codex configuration file. The selected provider still needs to support the Responses API used by HarnessPilot.

## Tests

Run deterministic HarnessPilot and pinned upstream regression suites with `bash scripts/test.sh` from the HarnessPilot source directory. The Linux environment needs bubblewrap for the namespace test. Tests send no provider requests.

## Verify

```powershell
$codexCli = Get-ChildItem "$env:CODEX_HOME\packages\official-*\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe" |
    Sort-Object FullName -Descending | Select-Object -First 1
& $codexCli.FullName plugin list
wsl.exe --distribution Ubuntu --user root --exec /usr/bin/systemd-run --system --pipe --wait --collect --quiet --uid=harnesspilot --property=Delegate=yes --property=CPUAccounting=yes --property=MemoryAccounting=yes --property=TasksAccounting=yes --property=CPUQuota=200% --property=MemoryMax=2147483648 --property=MemorySwapMax=0 --property=TasksMax=96 --setenv=HARNESSPILOT_CODEX_HOME=/mnt/e/CodexHome --setenv=HARNESSPILOT_STATE=/var/lib/harnesspilot/state --setenv=HARNESSPILOT_CODEX_BIN=/opt/harnesspilot/codex-cli/node_modules/.bin/codex --setenv=HARNESSPILOT_CODEX_ROOT=/opt/harnesspilot/codex-cli --setenv=PYTHONPATH=/opt/harnesspilot/app/upstream:/opt/harnesspilot/app -- /opt/harnesspilot/.venv/bin/python -m harnesspilot doctor
```

The first command should show `harnesspilot@local-marketplace` as installed and enabled. `doctor` prints the active model/provider identifiers and suite fingerprint, never credential values, and executes a no-model bubblewrap isolation probe. Installation does not start an experiment or contact the model provider.

## Authentication and runtime

The service requires `auth_mode = "apikey"` (or an API key in the current `auth.json`) and a Responses API provider in the selected `config.toml`. OAuth session tokens are not converted to API keys. The service reads the original files without editing or copying them. Provider traffic is made by the broker after explicit experiment approval. The Codex child has an isolated user, filesystem, process and network namespace; its only model path is the bounded Unix-socket broker.

No USD price hard limit is available. Request, token, output, and elapsed-time limits are operational bounds and do not guarantee a provider charge maximum. Always review the estimate and provider pricing before approval.

## Remove

Use the same managed CLI and Codex home to remove the plugin:

```powershell
$codexCli = Get-ChildItem "$env:CODEX_HOME\packages\official-*\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe" |
    Sort-Object FullName -Descending | Select-Object -First 1
& $codexCli.FullName plugin remove harnesspilot@local-marketplace
```

This removes the plugin registration/cache. The WSL runtime, state database, and timestamped previous plugin source are retained for manual review; remove them only after preserving any experiment records you need.
