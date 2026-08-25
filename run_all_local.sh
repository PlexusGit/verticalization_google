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

export APP_LOCAL_PORT="${APP_LOCAL_PORT:-8080}"
export TRANSCODE_LOCAL_PORT="${TRANSCODE_LOCAL_PORT:-8081}"
export VERTICAL_RENDER_LOCAL_PORT="${VERTICAL_RENDER_LOCAL_PORT:-8082}"
export GCS_IMPORT_LOCAL_PORT="${GCS_IMPORT_LOCAL_PORT:-8083}"
export S3_IMPORT_LOCAL_PORT="${S3_IMPORT_LOCAL_PORT:-8084}"
export LOCAL_RELOAD=0

port_is_free() {
  "${PYTHON}" - "$1" <<'PY'
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(1)
PY
}

for port in "${APP_LOCAL_PORT}" "${TRANSCODE_LOCAL_PORT}" "${VERTICAL_RENDER_LOCAL_PORT}" "${GCS_IMPORT_LOCAL_PORT}" "${S3_IMPORT_LOCAL_PORT}"; do
  if ! port_is_free "${port}"; then
    echo "A porta local ${port} já está em uso. Encerre o processo ou altere a variável *_LOCAL_PORT correspondente." >&2
    exit 1
  fi
done

PIDS=()
NAMES=()
CLEANED=0

cleanup() {
  [[ "${CLEANED}" == "0" ]] || return
  CLEANED=1
  echo
  echo "Encerrando serviços locais…"
  local pid
  for pid in "${PIDS[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
  for pid in "${PIDS[@]}"; do
    wait "${pid}" 2>/dev/null || true
  done
}

on_signal() {
  cleanup
  exit 130
}

trap cleanup EXIT
trap on_signal INT TERM

start_service() {
  local name="$1" script="$2"
  echo "Iniciando ${name}…"
  "${script}" &
  local pid=$!
  PIDS+=("${pid}")
  NAMES+=("${name}")
  sleep 0.5
  if ! kill -0 "${pid}" 2>/dev/null; then
    wait "${pid}" || true
    echo "Falha ao iniciar ${name}." >&2
    exit 1
  fi
}

start_service "transcode" "${ROOT}/transcode_function/run_local.sh"
start_service "vertical-render" "${ROOT}/vertical_render_function/run_local.sh"
start_service "app" "${ROOT}/run_local.sh"
start_service "process-gcs-bucket" "${ROOT}/functions/process_gcs_bucket/run_local.sh"
start_service "process-s3-bucket" "${ROOT}/functions/process_S3_bucket/run_local.sh"

echo
echo "Todos os serviços estão em execução:"
echo "  App:             http://127.0.0.1:${APP_LOCAL_PORT}"
echo "  API docs:        http://127.0.0.1:${APP_LOCAL_PORT}/docs"
echo "  Transcode:       http://127.0.0.1:${TRANSCODE_LOCAL_PORT}"
echo "  Vertical render: http://127.0.0.1:${VERTICAL_RENDER_LOCAL_PORT}/health"
echo "  GCS importer:    http://127.0.0.1:${GCS_IMPORT_LOCAL_PORT}"
echo "  S3 importer:     http://127.0.0.1:${S3_IMPORT_LOCAL_PORT}"
echo "Pressione Ctrl+C para encerrar todos."

while true; do
  for index in "${!PIDS[@]}"; do
    if ! kill -0 "${PIDS[$index]}" 2>/dev/null; then
      wait "${PIDS[$index]}" || status=$?
      echo "O serviço ${NAMES[$index]} encerrou inesperadamente (status ${status:-0})." >&2
      exit "${status:-1}"
    fi
  done
  sleep 1
done
