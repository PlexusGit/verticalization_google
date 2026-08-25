#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

# The project contract places deployment configuration at verticalization_gni/.env.
# Only simple KEY=VALUE records are accepted; arbitrary shell is never evaluated.
if [[ -f "${ENV_FILE}" ]]; then
  echo "Loading ${ENV_FILE}"
  while IFS='=' read -r key value; do
    [[ -z "${key}" || "${key}" =~ ^[[:space:]]*# ]] && continue
    key="$(echo "${key}" | xargs)"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="${value%$'\r'}"; value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
    export "${key}=${value}"
  done < "${ENV_FILE}"
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?GOOGLE_CLOUD_PROJECT must be set in ../.env}"
REGION="${VERTICAL_RENDER_REGION:-${DEPLOY_REGION:-southamerica-east1}}"
SERVICE="${VERTICAL_RENDER_SERVICE_NAME:-gni-vertical-render}"
REPOSITORY="${ARTIFACT_REGISTRY_REPOSITORY:-plexus-vertical-cuts}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}:${IMAGE_TAG:-latest}"
BUCKET="${BUCKET_NAME:?BUCKET_NAME must be set in ../.env}"
COLLECTION="${COLLECTION_VIDEOS:-gni_videos}"
API_KEY_SECRET="${SECURITY_TOKEN_SECRET:-plexus-vertical-cuts-api-token}"
SERVICE_ACCOUNT="${VERTICAL_RENDER_SERVICE_ACCOUNT:-${SERVICE_ACCOUNT_EMAIL:-}}"
[[ -n "${API_KEY_SECRET}" ]] || { echo "SECURITY_TOKEN_SECRET must be set" >&2; exit 1; }
[[ -n "${SERVICE_ACCOUNT}" ]] || { echo "VERTICAL_RENDER_SERVICE_ACCOUNT must be set" >&2; exit 1; }

printf 'Deploying %s\nImage: %s\n' "${SERVICE}" "${IMAGE}"
gcloud config set project "${PROJECT_ID}"
if ! gcloud artifacts repositories describe "${REPOSITORY}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" --repository-format=docker --location="${REGION}" --description="GNI render workers"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build --platform linux/amd64 -t "${IMAGE}" "${SCRIPT_DIR}"
docker push "${IMAGE}"

gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" --region="${REGION}" --platform=managed \
  --allow-unauthenticated --service-account="${SERVICE_ACCOUNT}" \
  --cpu=4 --memory=8Gi --timeout=3600 --concurrency=1 \
  --min-instances=0 --max-instances="${VERTICAL_RENDER_MAX_INSTANCES:-5}" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},BUCKET_NAME=${BUCKET},COLLECTION_VIDEOS=${COLLECTION}" \
  --set-secrets="API_KEY=${API_KEY_SECRET}:latest"

gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)'
