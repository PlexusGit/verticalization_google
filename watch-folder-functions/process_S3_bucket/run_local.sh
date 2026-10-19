#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=../../scripts/lib.sh
source "${ROOT}/scripts/lib.sh"
load_env

PYTHON="${LOCAL_PYTHON:-${ROOT}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || {
  echo "Virtualenv ausente. Instale requirements-test.txt no .venv da raiz." >&2
  exit 1
}
PORT="${S3_IMPORT_LOCAL_PORT:-8084}"
APP_PORT="${APP_LOCAL_PORT:-8080}"
export PORT
export MAIN_API_URL="http://127.0.0.1:${APP_PORT}"
export API_TOKEN="${SECURITY_TOKEN}"
export GCS_MAIN_BUCKET="${GCS_MAIN_BUCKET:-${BUCKET_NAME}}"
export AWS_REGION="${S3_REGION:-us-east-1}"

if [[ -z "${S3_WATCH_BUCKET:-}" ]]; then
  echo "Aviso: S3_WATCH_BUCKET está vazio; o processo inicia, mas eventos serão ignorados." >&2
fi
echo "S3 Pub/Sub function: http://127.0.0.1:${PORT}"
cd "${SCRIPT_DIR}"
exec "${PYTHON}" "${ROOT}/scripts/run_function_local.py" --target=process_s3_bucket --source="${SCRIPT_DIR}/main.py" --signature-type=cloudevent --host=127.0.0.1 --port="${PORT}"
