param(
    [Parameter(Mandatory = $true)] [string] $CodexHome,
    [Parameter(Mandatory = $true)] [string] $CodexCliPath,
    [string] $Distribution = "Ubuntu"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = if (Test-Path -LiteralPath (Join-Path $repo ".codex-plugin\plugin.json")) {
    $repo
} else {
    Join-Path $repo "plugin"
}
$marketplace = Join-Path $HOME ".agents\plugins\marketplace.json"

if (-not (Test-Path -LiteralPath $marketplace -PathType Leaf)) {
    throw "Personal plugin marketplace not found: $marketplace"
}
if (-not (Test-Path -LiteralPath (Join-Path $CodexHome "config.toml") -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $CodexHome "auth.json") -PathType Leaf)) {
    throw "The selected Codex home must contain config.toml and auth.json."
}

$listing = Get-Content -LiteralPath $marketplace -Raw | ConvertFrom-Json
$entry = $listing.plugins | Where-Object { $_.name -eq "harnesspilot" } | Select-Object -First 1
if (-not $entry -or $entry.source.source -ne "local") {
    throw "The personal marketplace has no local HarnessPilot source entry."
}
$destination = [IO.Path]::GetFullPath((Join-Path $HOME $entry.source.path))
$parent = Split-Path -Parent $destination
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$stage = "$destination.next-$PID"
if (Test-Path -LiteralPath $stage) {
    throw "Install staging directory already exists: $stage"
}
New-Item -ItemType Directory -Path $stage | Out-Null
Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force
}

$linuxHome = (& wsl.exe --distribution $Distribution --user root --exec wslpath -a $CodexHome | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxHome.StartsWith("/")) {
    throw "Could not resolve the selected Codex home inside WSL."
}
$mcpPath = Join-Path $stage ".mcp.json"
$mcp = Get-Content -LiteralPath $mcpPath -Raw | ConvertFrom-Json
$mcp.mcpServers.harnesspilot.args[1] = $Distribution
$homeArgument = -1
for ($index = 0; $index -lt $mcp.mcpServers.harnesspilot.args.Count; $index++) {
    if ($mcp.mcpServers.harnesspilot.args[$index] -like "--setenv=HARNESSPILOT_CODEX_HOME=*") {
        $homeArgument = $index
        break
    }
}
if ($homeArgument -lt 0) {
    throw "The plugin MCP configuration has no Codex home environment argument."
}
$mcp.mcpServers.harnesspilot.args[$homeArgument] = "--setenv=HARNESSPILOT_CODEX_HOME=$linuxHome"
[IO.File]::WriteAllText($mcpPath, ($mcp | ConvertTo-Json -Depth 20),
    [Text.UTF8Encoding]::new($false))

$modelFingerprintProgram = @'
import hashlib, json, sys, tomllib
config = tomllib.load(open(sys.argv[1], "rb"))
provider = config.get("model_provider", "openai")
settings = config.get("model_providers", {}).get(provider, {})
model_keys = ("model", "model_provider", "model_reasoning_effort", "model_context_window",
              "model_auto_compact_token_limit", "model_verbosity", "model_reasoning_summary",
              "model_supports_reasoning_summaries", "service_tier")
provider_keys = ("wire_api", "base_url", "env_key", "http_headers", "env_http_headers",
                 "query_params")
effective = {"model": {key: config[key] for key in model_keys if key in config},
             "provider": {key: settings[key] for key in provider_keys if key in settings}}
payload = json.dumps(effective, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
'@
function Get-ModelSettingsFingerprint {
    $result = & wsl.exe --distribution $Distribution --user root --exec /usr/bin/python3.12 `
        -c $modelFingerprintProgram "$linuxHome/config.toml" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not fingerprint the selected Codex model settings."
    }
    $fingerprint = ($result | Out-String).Trim()
    if ($fingerprint -notmatch "^[0-9a-f]{64}$") {
        throw "The selected Codex model settings returned an invalid fingerprint."
    }
    return $fingerprint
}

try {
    $creator = Join-Path $CodexHome "skills\.system\plugin-creator\scripts\update_plugin_cachebuster.py"
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ((Test-Path -LiteralPath $creator -PathType Leaf) -and $python) {
        & $python.Source $creator $stage
        if ($LASTEXITCODE -ne 0) {
            throw "Plugin cachebuster update failed."
        }
    }

    $backup = ""
    if (Test-Path -LiteralPath $destination) {
        $backup = "$destination.backup-$(Get-Date -Format yyyyMMdd-HHmmss)-$PID"
        Move-Item -LiteralPath $destination -Destination $backup
    }
    Move-Item -LiteralPath $stage -Destination $destination

    $modelSettingsHash = Get-ModelSettingsFingerprint
    $authHash = (Get-FileHash -LiteralPath (Join-Path $CodexHome "auth.json") -Algorithm SHA256).Hash
    $previousHome = $env:CODEX_HOME
    try {
        $env:CODEX_HOME = $CodexHome
        & $CodexCliPath plugin add harnesspilot@local-marketplace
        if ($LASTEXITCODE -ne 0) {
            throw "Codex plugin installation failed. Existing plugin copy: $backup"
        }
        $pluginStatus = & $CodexCliPath plugin list 2>&1 |
            Select-String "harnesspilot@local-marketplace" | Select-Object -First 1
        if (-not $pluginStatus -or $pluginStatus.ToString() -notmatch "installed, enabled") {
            throw "HarnessPilot did not reach installed, enabled state."
        }
    }
    finally {
        $env:CODEX_HOME = $previousHome
    }

    if ((Get-ModelSettingsFingerprint) -ne $modelSettingsHash) {
        throw "Codex model/provider settings changed during plugin installation."
    }
    if ((Get-FileHash -LiteralPath (Join-Path $CodexHome "auth.json") -Algorithm SHA256).Hash -ne $authHash) {
        throw "Codex authentication changed during plugin installation."
    }
    Write-Output $pluginStatus.ToString().Trim()
    if ($backup) {
        Write-Output "Previous plugin source preserved at $backup"
    }
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
