#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${ROOT}/scripts/lib.sh"
load_env
require_vars GOOGLE_CLOUD_PROJECT DEPLOY_REGION

echo "1/5 — Transcodificador"
"${ROOT}/transcode_function/deploy.sh"
TRANSCODE_DEPLOYED_URL="$(service_url "${TRANSCODE_SERVICE_NAME:-plexus-vertical-cuts-transcode}" "${TRANSCODE_REGION:-${DEPLOY_REGION}}")"
upsert_env TRANSCODE_URL "${TRANSCODE_DEPLOYED_URL}"

echo "2/5 — Renderizador vertical"
"${ROOT}/vertical_render_function/deploy.sh"
RENDER_DEPLOYED_URL="$(service_url "${VERTICAL_RENDER_SERVICE_NAME:-plexus-vertical-cuts-render}" "${VERTICAL_RENDER_REGION:-${DEPLOY_REGION}}")"
upsert_env VERTICAL_RENDER_URL "${RENDER_DEPLOYED_URL}"

echo "3/5 — Aplicação principal"
APP_URL="$("${ROOT}/deploy.sh" | tail -n1)"
upsert_env CLOUD_RUN_DOMAIN "${APP_URL}"
upsert_env DOMAIN "${APP_URL}"
upsert_env MAIN_API_URL "${APP_URL}"
upsert_env GCS_MAIN_BUCKET "${BUCKET_NAME}"

# Redeploy once so the signed session cookie and generated URLs use the final domain.
"${ROOT}/deploy.sh" >/dev/null

echo "4/5 — Importador GCS"
if [[ -n "${GCS_WATCH_BUCKET:-}" ]]; then "${ROOT}/watch-folder-functions/process_gcs_bucket/deploy.sh"; else echo "Ignorado: GCS_WATCH_BUCKET vazio."; fi

echo "5/5 — Importador S3"
if [[ -n "${S3_WATCH_BUCKET:-}" && -n "${AWS_ACCESS_KEY_ID_SECRET:-}" && -n "${AWS_SECRET_ACCESS_KEY_SECRET:-}" ]]; then "${ROOT}/watch-folder-functions/process_S3_bucket/deploy.sh"; else echo "Ignorado: configuração S3 incompleta."; fi

CORS_FILE="$(mktemp)"; trap 'rm -f "${CORS_FILE}"' EXIT
printf '[{"origin":["%s","http://localhost:8080"],"method":["GET","HEAD","PUT","POST"],"responseHeader":["Content-Type","Content-Length","Range"],"maxAgeSeconds":3600}]' "${APP_URL}" > "${CORS_FILE}"
gcloud storage buckets update "gs://${BUCKET_NAME}" --cors-file "${CORS_FILE}" --project "${GOOGLE_CLOUD_PROJECT}" >/dev/null

echo "Plexus Vertical Cuts implantado em ${APP_URL}"
echo "Adicione ${APP_URL} às origens JavaScript autorizadas do cliente OAuth."
