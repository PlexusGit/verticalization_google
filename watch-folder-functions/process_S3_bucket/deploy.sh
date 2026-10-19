#!/usr/bin/env bash
set -euo pipefail

# Read non-secret deployment settings and Secret Manager resource names only
# from the single verticalization_gni root .env file.
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
: "${S3_WATCH_BUCKET:?Set S3_WATCH_BUCKET in ${ENV_FILE}}"
: "${GCS_MAIN_BUCKET:?Set GCS_MAIN_BUCKET in ${ENV_FILE}}"
: "${S3_PUBSUB_TOPIC:?Set S3_PUBSUB_TOPIC in ${ENV_FILE}}"
: "${API_TOKEN_SECRET:?Set API_TOKEN_SECRET in ${ENV_FILE}}"
: "${AWS_ACCESS_KEY_ID_SECRET:?Set AWS_ACCESS_KEY_ID_SECRET in ${ENV_FILE}}"
: "${AWS_SECRET_ACCESS_KEY_SECRET:?Set AWS_SECRET_ACCESS_KEY_SECRET in ${ENV_FILE}}"

FUNCTION_NAME="${S3_PROCESS_FUNCTION_NAME:-process-s3-bucket}"
REGION="${S3_PROCESS_REGION:-${DEPLOY_REGION:-us-central1}}"
RUNTIME="${FUNCTION_RUNTIME:-python312}"
EXTENSIONS="${S3_ALLOWED_EXTENSIONS:-.mp4,.mov,.mkv,.webm,.avi,.mxf}"
AWS_REGION_VALUE="${S3_REGION:-${AWS_REGION:-us-east-1}}"
GCS_PREFIX="${S3_GCS_PREFIX:-imports/s3}"
# The copy is streamed S3 -> GCS, so memory does not scale with video size.
# Event-driven Gen2 functions cannot run longer than 540 s.
FUNCTION_MEMORY="${S3_FUNCTION_MEMORY:-1Gi}"
FUNCTION_TIMEOUT="${S3_FUNCTION_TIMEOUT:-540s}"
SECRET_BINDINGS="API_TOKEN=${API_TOKEN_SECRET}:latest,AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID_SECRET}:latest,AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY_SECRET}:latest"
if [[ -n "${AWS_SESSION_TOKEN_SECRET:-}" ]]; then
  SECRET_BINDINGS="${SECRET_BINDINGS},AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN_SECRET}:latest"
fi
SERVICE_ACCOUNT_ARG=()
FUNCTION_SERVICE_ACCOUNT="${S3_PROCESS_SERVICE_ACCOUNT:-${SERVICE_ACCOUNT_EMAIL:-}}"
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
  --entry-point process_s3_bucket \
  --trigger-topic "${S3_PUBSUB_TOPIC}" \
  --retry \
  --memory "${FUNCTION_MEMORY}" \
  --timeout "${FUNCTION_TIMEOUT}" \
  --docker-registry artifact-registry \
  --set-env-vars "^@^MAIN_API_URL=${MAIN_API_URL}@S3_WATCH_BUCKET=${S3_WATCH_BUCKET}@GCS_MAIN_BUCKET=${GCS_MAIN_BUCKET}@S3_ALLOWED_EXTENSIONS=${EXTENSIONS}@AWS_REGION=${AWS_REGION_VALUE}@S3_GCS_PREFIX=${GCS_PREFIX}" \
  --set-secrets "${SECRET_BINDINGS}" \
  ${SERVICE_ACCOUNT_ARG[@]+"${SERVICE_ACCOUNT_ARG[@]}"}
