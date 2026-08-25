#!/usr/bin/env bash
# Shared strict helpers for all root automation. This file is sourced, not run.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

load_env() {
  [[ -f "${ENV_FILE}" ]] || { echo "Arquivo ausente: ${ENV_FILE}. Copie .env.example para .env." >&2; return 1; }
  while IFS='=' read -r key value; do
    [[ -z "${key}" || "${key}" =~ ^[[:space:]]*# ]] && continue
    key="$(echo "${key}" | xargs)"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="${value%$'\r'}"; value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
    # Google Auth interprets even an empty credentials variable as a file path.
    # Unset it so ADC is used when no local JSON key was configured.
    if [[ "${key}" == "GOOGLE_APPLICATION_CREDENTIALS" && -z "${value}" ]]; then
      unset GOOGLE_APPLICATION_CREDENTIALS
      continue
    fi
    export "${key}=${value}"
  done < "${ENV_FILE}"
}

require_vars() {
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || { echo "Variável obrigatória ausente: ${name}" >&2; return 1; }
  done
}

require_tools() {
  local tool
  for tool in "$@"; do command -v "${tool}" >/dev/null || { echo "Ferramenta obrigatória ausente: ${tool}" >&2; return 1; }; done
}

upsert_env() {
  local key="$1" value="$2" temporary
  temporary="$(mktemp)"
  awk -v key="${key}" -v value="${value}" 'BEGIN{done=0} $0 ~ "^"key"=" {print key"="value; done=1; next} {print} END{if(!done) print key"="value}' "${ENV_FILE}" > "${temporary}"
  mv "${temporary}" "${ENV_FILE}"
  export "${key}=${value}"
}

service_url() {
  gcloud run services describe "$1" --region "$2" --project "${GOOGLE_CLOUD_PROJECT}" --format='value(status.url)'
}
