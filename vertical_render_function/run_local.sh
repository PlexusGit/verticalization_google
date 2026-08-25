#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=../scripts/lib.sh
source "${ROOT}/scripts/lib.sh"
load_env

PYTHON="${LOCAL_PYTHON:-${ROOT}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || {
  echo "Virtualenv ausente. Instale requirements-test.txt no .venv da raiz." >&2
  exit 1
}
PORT="${VERTICAL_RENDER_LOCAL_PORT:-8082}"
export PORT
export API_KEY="${SECURITY_TOKEN}"

# The deployed worker receives API_KEY from Secret Manager; locally it is
# mapped from the same root SECURITY_TOKEN used by the app client.
echo "Vertical render worker: http://127.0.0.1:${PORT}"
cd "${SCRIPT_DIR}"
# Avoid Gunicorn's pre-fork model locally; Cloud Run still uses its production CMD.
exec "${PYTHON}" "${ROOT}/scripts/run_function_local.py" --target=vertical_render --source="${SCRIPT_DIR}/main.py" --host=127.0.0.1 --port="${PORT}"
