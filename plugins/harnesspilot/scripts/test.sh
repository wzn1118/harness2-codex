#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -d "$SOURCE/upstream/harness2" ]]; then
  UPSTREAM_SOURCE="$SOURCE/upstream"
else
  UPSTREAM_SOURCE="$SOURCE/../harness2-codex/vendor/harness2-upstream"
fi
[[ -d "$UPSTREAM_SOURCE/harness2" ]] || {
  printf 'Harness2 source was not found at %s.\n' "$UPSTREAM_SOURCE" >&2
  exit 2
}

cd "$SOURCE"
export PYTHONPATH="$SOURCE:$UPSTREAM_SOURCE${PYTHONPATH:+:$PYTHONPATH}"
if [[ -x /opt/harnesspilot/.venv/bin/python ]]; then
  PYTHON_BIN="/opt/harnesspilot/.venv/bin/python"
elif [[ -x "$SOURCE/.venv/bin/python" ]]; then
  PYTHON_BIN="$SOURCE/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="python3.12"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  printf 'Python 3.12 or python was not found.\n' >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import mcp, yaml, tomlkit' >/dev/null 2>&1; then
  "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input -e "$SOURCE"
fi
"$PYTHON_BIN" -m unittest discover -s tests -v
"$PYTHON_BIN" -m unittest discover -s "$UPSTREAM_SOURCE/tests" -v
