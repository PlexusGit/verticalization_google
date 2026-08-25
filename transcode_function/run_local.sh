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
PORT="${TRANSCODE_LOCAL_PORT:-8081}"
export PORT
export USE_NVENC="${LOCAL_USE_NVENC:-0}"

# CPU is the predictable local default. Set LOCAL_USE_NVENC=1 only on a host
# with NVIDIA drivers and an ffmpeg build that includes h264_nvenc.
echo "Transcode worker: http://127.0.0.1:${PORT} (NVENC=${USE_NVENC})"
cd "${SCRIPT_DIR}"
# Use the single-process local launcher to avoid Gunicorn pre-fork crashes in
# Objective-C-backed Google libraries on macOS.
exec "${PYTHON}" "${ROOT}/scripts/run_function_local.py" --target=process_transcode --source="${SCRIPT_DIR}/main.py" --host=127.0.0.1 --port="${PORT}"
