#!/usr/bin/env bash
set -euo pipefail

# All deployment configuration lives in the one verticalization_gni root .env.
# Runtime secrets are referenced by Secret Manager name and are never copied
# from .env into plain Cloud Function environment variables.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing deployment configuration: ${ENV_FILE}" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in ${ENV_FILE}}"
: "${MAIN_API_URL:?Set MAIN_API_URL in ${ENV_FILE}}"
: "${GCS_WATCH_BUCKET:?Set GCS_WATCH_BUCKET in ${ENV_FILE}}"
: "${GCS_PUBSUB_TOPIC:?Set GCS_PUBSUB_TOPIC in ${ENV_FILE}}"
: "${API_TOKEN_SECRET:?Set API_TOKEN_SECRET (a Secret Manager secret name) in ${ENV_FILE}}"

FUNCTION_NAME="${GCS_PROCESS_FUNCTION_NAME:-process-gcs-bucket}"
REGION="${GCS_PROCESS_REGION:-${DEPLOY_REGION:-us-central1}}"
RUNTIME="${FUNCTION_RUNTIME:-python312}"
EXTENSIONS="${GCS_ALLOWED_EXTENSIONS:-.mp4,.mov,.mkv,.avi,.webm,.m4v,.mpg,.mpeg}"
SERVICE_ACCOUNT_ARG=()
FUNCTION_SERVICE_ACCOUNT="${GCS_PROCESS_SERVICE_ACCOUNT:-${SERVICE_ACCOUNT_EMAIL:-}}"
if [[ -n "${FUNCTION_SERVICE_ACCOUNT}" ]]; then
  SERVICE_ACCOUNT_ARG=(--service-account "${FUNCTION_SERVICE_ACCOUNT}")
fi

echo "Deploying ${FUNCTION_NAME} from source to ${GOOGLE_CLOUD_PROJECT}/${REGION}"
gcloud functions deploy "${FUNCTION_NAME}" \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${REGION}" \
  --gen2 \
  --runtime "${RUNTIME}" \
  --source "${SCRIPT_DIR}" \
  --entry-point process_gcs_bucket \
  --trigger-topic "${GCS_PUBSUB_TOPIC}" \
  --retry \
  --docker-registry artifact-registry \
  --set-env-vars "^@^MAIN_API_URL=${MAIN_API_URL}@GCS_WATCH_BUCKET=${GCS_WATCH_BUCKET}@GCS_ALLOWED_EXTENSIONS=${EXTENSIONS}" \
  --set-secrets "API_TOKEN=${API_TOKEN_SECRET}:latest" \
  ${SERVICE_ACCOUNT_ARG[@]+"${SERVICE_ACCOUNT_ARG[@]}"}
