#!/usr/bin/env bash
set -euo pipefail

# Optional Git Bash entry point. The exporter itself requires native Windows.
# No default source directories, Git archive fallback or privacy bypass exists.
if [[ -z "${CGF_NATIVE_PYTHON:-}" ]]; then
  echo 'Set CGF_NATIVE_PYTHON to an explicit native Windows Python executable; all exporter arguments are required.' >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$CGF_NATIVE_PYTHON" "$SCRIPT_DIR/../tools/prepare_public_source.py" "$@"
