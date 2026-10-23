#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'Run this installer as WSL root. The MCP service runs as its own account.\n' >&2
  exit 2
fi

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_VERSION="${HARNESSPILOT_CODEX_VERSION:-}"
CODEX_HOME="${HARNESSPILOT_CODEX_HOME:-}"
STATE_ROOT="${HARNESSPILOT_STATE:-}"
UPSTREAM_SOURCE="${HARNESSPILOT_UPSTREAM_SOURCE:-}"

while (($#)); do
  case "$1" in
    --codex-version) CODEX_VERSION="$2"; shift 2 ;;
    --codex-home) CODEX_HOME="$2"; shift 2 ;;
    --state-root) STATE_ROOT="$2"; shift 2 ;;
    --upstream-source) UPSTREAM_SOURCE="$2"; shift 2 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ "$CODEX_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  printf 'Pass the active Codex Desktop CLI version with --codex-version.\n' >&2
  exit 2
}
[[ -n "$CODEX_HOME" && -r "$CODEX_HOME/config.toml" && -r "$CODEX_HOME/auth.json" ]] || {
  printf 'Codex config.toml and auth.json must be readable at the selected CODEX_HOME.\n' >&2
  exit 2
}
[[ -n "$STATE_ROOT" ]] || STATE_ROOT="/var/lib/harnesspilot/state"

if [[ -z "$UPSTREAM_SOURCE" ]]; then
  if [[ -d "$SOURCE/upstream/harness2" ]]; then
    UPSTREAM_SOURCE="$SOURCE/upstream"
  else
    UPSTREAM_SOURCE="$SOURCE/../harness2-codex/vendor/harness2-upstream"
  fi
fi
[[ -f "$UPSTREAM_SOURCE/pyproject.toml" && -d "$UPSTREAM_SOURCE/harness2" ]] || {
  printf 'Harness2 source was not found at %s.\n' "$UPSTREAM_SOURCE" >&2
  exit 2
}

missing=()
command -v python3.12 >/dev/null 2>&1 || missing+=(python3.12)
command -v bwrap >/dev/null 2>&1 || missing+=(bubblewrap)
command -v node >/dev/null 2>&1 || missing+=(nodejs)
command -v npm >/dev/null 2>&1 || missing+=(npm)
command -v rustc >/dev/null 2>&1 || missing+=(rustc)
command -v runuser >/dev/null 2>&1 || missing+=(util-linux)
if ((${#missing[@]})); then
  apt-get update
  apt-get install -y "${missing[@]}" python3.12-venv
fi

for command_name in python3.12 bwrap node npm rustc runuser; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Required command is missing after package installation: %s\n' "$command_name" >&2
    exit 2
  }
done

if ! getent passwd harnesspilot >/dev/null; then
  useradd --system --create-home --home-dir /var/lib/harnesspilot \
    --shell /usr/sbin/nologin harnesspilot
fi
SERVICE_UID="$(id -u harnesspilot)"
[[ "$SERVICE_UID" -ne 0 ]] || { printf 'Refusing to run MCP as root.\n' >&2; exit 2; }

RUNTIME=/opt/harnesspilot
APP="$RUNTIME/app"
VENV="$RUNTIME/.venv"
CODEX_ROOT="$RUNTIME/codex-cli"
CODEX_BIN="$CODEX_ROOT/node_modules/.bin/codex"

install -d -m 0755 "$RUNTIME" "$APP" "$APP/harnesspilot" "$APP/benchmarks" \
  "$APP/upstream" "$CODEX_ROOT"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3.12 -m venv "$VENV"
fi

cp -a "$SOURCE/harnesspilot/." "$APP/harnesspilot/"
cp -a "$SOURCE/benchmarks/." "$APP/benchmarks/"
cp -a "$SOURCE/pyproject.toml" "$SOURCE/LICENSE" "$SOURCE/README.md" "$APP/"
cp -a "$UPSTREAM_SOURCE/." "$APP/upstream/"
"$VENV/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
  --editable "$APP" --editable "$APP/upstream"

npm install --prefix "$CODEX_ROOT" --no-audit --no-fund --no-package-lock \
  --save-exact "@openai/codex@$CODEX_VERSION"
[[ -x "$CODEX_BIN" ]] || { printf 'Codex CLI install did not create %s.\n' "$CODEX_BIN" >&2; exit 2; }
VERSION_OUTPUT="$("$CODEX_BIN" --version)"
[[ "$VERSION_OUTPUT" == *"codex-cli $CODEX_VERSION"* ]] || {
  printf 'Installed CLI version mismatch: expected %s, received %s.\n' \
    "$CODEX_VERSION" "$VERSION_OUTPUT" >&2
  exit 2
}

chown -R root:root "$RUNTIME"
chmod -R go-w "$RUNTIME"
install -d -o harnesspilot -g harnesspilot -m 0700 "$STATE_ROOT"

runuser -u harnesspilot -- test -r "$CODEX_HOME/config.toml"
runuser -u harnesspilot -- test -r "$CODEX_HOME/auth.json"
runuser -u harnesspilot -- test -w "$STATE_ROOT" || {
  printf 'The service account cannot write its state directory: %s\n' "$STATE_ROOT" >&2
  exit 2
}

systemd-run --system --pipe --wait --collect --quiet --uid=harnesspilot \
  --property=Delegate=yes --property=CPUAccounting=yes --property=MemoryAccounting=yes \
  --property=TasksAccounting=yes --property=CPUQuota=200% \
  --property=MemoryMax=2147483648 --property=MemorySwapMax=0 --property=TasksMax=96 \
  "--setenv=HARNESSPILOT_CODEX_HOME=$CODEX_HOME" \
  "--setenv=HARNESSPILOT_STATE=$STATE_ROOT" \
  "--setenv=HARNESSPILOT_CODEX_BIN=$CODEX_BIN" \
  "--setenv=HARNESSPILOT_CODEX_ROOT=$CODEX_ROOT" \
  "--setenv=PYTHONPATH=$APP/upstream:$APP" \
  -- "$VENV/bin/python" -m harnesspilot doctor
