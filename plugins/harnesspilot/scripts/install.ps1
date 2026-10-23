param(
    [string] $CodexHome = $env:CODEX_HOME,
    [string] $Distribution = "Ubuntu",
    [string] $CodexCliPath = ""
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $CodexHome) {
    $CodexHome = Join-Path $HOME ".codex"
}
if (-not (Test-Path -LiteralPath (Join-Path $CodexHome "config.toml") -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $CodexHome "auth.json") -PathType Leaf)) {
    throw "Pass -CodexHome pointing to the current Codex config.toml and auth.json directory."
}

if (-not $CodexCliPath) {
    $packageRoot = Join-Path $CodexHome "packages"
    $candidates = foreach ($package in Get-ChildItem -LiteralPath $packageRoot -Directory -Filter "official-*" -ErrorAction SilentlyContinue) {
        if ($package.Name -match "^official-(\d+\.\d+\.\d+)$") {
            $binary = Join-Path $package.FullName "node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"
            if (Test-Path -LiteralPath $binary -PathType Leaf) {
                [pscustomobject]@{ Version = [version]$Matches[1]; Path = $binary }
            }
        }
    }
    $selected = $candidates | Sort-Object Version -Descending | Select-Object -First 1
    if (-not $selected) {
        throw "Could not find a managed Codex Desktop CLI under $packageRoot. Pass -CodexCliPath explicitly."
    }
    $CodexCliPath = $selected.Path
}

$versionOutput = & $CodexCliPath --version | Out-String
if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch "codex-cli\s+(\d+\.\d+\.\d+)") {
    throw "The selected Codex CLI did not report a supported semantic version."
}
$codexVersion = $Matches[1]
$linuxRepo = (& wsl.exe --distribution $Distribution --user root --exec wslpath -a $repo | Out-String).Trim()
$linuxCodexHome = (& wsl.exe --distribution $Distribution --user root --exec wslpath -a $CodexHome | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxRepo.StartsWith("/") -or -not $linuxCodexHome.StartsWith("/")) {
    throw "Could not resolve the project or Codex home inside WSL."
}
$linuxState = "/var/lib/harnesspilot/state"
& wsl.exe --distribution $Distribution --user root --exec /bin/bash `
    "$linuxRepo/scripts/install-wsl.sh" --codex-version $codexVersion `
    --codex-home $linuxCodexHome --state-root $linuxState
if ($LASTEXITCODE -ne 0) {
    throw "WSL runtime installation failed."
}

& (Join-Path $PSScriptRoot "install-plugin.ps1") `
    -CodexHome $CodexHome -CodexCliPath $CodexCliPath -Distribution $Distribution
