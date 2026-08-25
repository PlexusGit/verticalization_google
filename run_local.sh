#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${ROOT}/scripts/lib.sh"
load_env

PYTHON="${LOCAL_PYTHON:-${ROOT}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || {
  echo "Ambiente virtual ausente. Execute: python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt" >&2
  exit 1
}

APP_PORT="${APP_LOCAL_PORT:-8080}"
TRANSCODE_PORT="${TRANSCODE_LOCAL_PORT:-8081}"
RENDER_PORT="${VERTICAL_RENDER_LOCAL_PORT:-8082}"
export DOMAIN="http://127.0.0.1:${APP_PORT}"
export TRANSCODE_URL="http://127.0.0.1:${TRANSCODE_PORT}"
export VERTICAL_RENDER_URL="http://127.0.0.1:${RENDER_PORT}"
export OAUTHLIB_INSECURE_TRANSPORT=1

echo "Aplicação: ${DOMAIN}"
echo "Transcode local: ${TRANSCODE_URL}"
echo "Render local: ${VERTICAL_RENDER_URL}"
cd "${ROOT}"
if [[ "${LOCAL_RELOAD:-1}" == "1" ]]; then
  exec "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${APP_PORT}" --reload
fi
exec "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${APP_PORT}"
