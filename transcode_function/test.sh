#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_ENV="${SCRIPT_DIR}/../.env"
if [[ -f "${PARENT_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${PARENT_ENV}"
  set +a
fi
cd "${SCRIPT_DIR}"
python3 -m compileall -q main.py processor.py tests
python3 -m pytest -q tests
