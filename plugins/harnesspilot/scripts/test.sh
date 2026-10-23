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
python -m unittest discover -s tests -v
python -m unittest discover -s "$UPSTREAM_SOURCE/tests" -v
