#!/usr/bin/env bash
set -Eeuo pipefail

# Build and deploy Plexus Vertical Cuts Transcode to Cloud Run via Artifact Registry.
# Configuration is inherited from verticalization_gni/.env when that file exists.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_ENV="${SCRIPT_DIR}/../.env"
if [[ -f "${PARENT_ENV}" ]]; then
  echo "Loading ${PARENT_ENV}"
  set -a
  # shellcheck disable=SC1090
  source "${PARENT_ENV}"
  set +a
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT_ID:-}}"
REGION="${TRANSCODE_REGION:-${DEPLOY_REGION:-${GOOGLE_CLOUD_REGION:-us-central1}}}"
REPOSITORY="${ARTIFACT_REGISTRY_REPOSITORY:-plexus-vertical-cuts}"
SERVICE_NAME="${TRANSCODE_SERVICE_NAME:-plexus-vertical-cuts-transcode}"
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d-%H%M%S)}"
BUCKET="${BUCKET_NAME:-}"
API_KEY_SECRET="${SECURITY_TOKEN_SECRET:-plexus-vertical-cuts-api-token}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT_EMAIL:-}"
USE_GPU="${TRANSCODE_USE_GPU:-1}"

for variable in PROJECT_ID BUCKET API_KEY_SECRET; do
  if [[ -z "${!variable}" ]]; then
    echo "Error: ${variable} is required (set it in ${PARENT_ENV} or the environment)." >&2
    exit 1
  fi
done

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE_NAME}:${IMAGE_TAG}"
gcloud config set project "${PROJECT_ID}"
if ! gcloud artifacts repositories describe "${REPOSITORY}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" \
    --repository-format=docker --location="${REGION}" \
    --description="Plexus Vertical Cuts container images"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "Building ${IMAGE}"
docker build --platform linux/amd64 --tag "${IMAGE}" "${SCRIPT_DIR}"
docker push "${IMAGE}"

ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},BUCKET_NAME=${BUCKET}"
GPU_ARGS=()
if [[ "${USE_GPU}" == "1" ]]; then
  GPU_ARGS=(--gpu=1 --gpu-type=nvidia-l4 --no-gpu-zonal-redundancy)
else
  ENV_VARS="${ENV_VARS},USE_NVENC=0"
fi
SERVICE_ACCOUNT_ARGS=()
if [[ -n "${SERVICE_ACCOUNT}" ]]; then
  SERVICE_ACCOUNT_ARGS=(--service-account="${SERVICE_ACCOUNT}")
fi

gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" --region="${REGION}" --platform=managed \
  --execution-environment=gen2 --allow-unauthenticated \
  "${SERVICE_ACCOUNT_ARGS[@]}" "${GPU_ARGS[@]}" \
  --cpu=4 --memory=16Gi --timeout=3600 --concurrency=1 \
  --min-instances=0 --max-instances=1 --no-cpu-throttling \
  --set-env-vars="${ENV_VARS}" \
  --set-secrets="SECURITY_TOKEN=${API_KEY_SECRET}:latest"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format='value(status.url)')"
echo "Deployed ${SERVICE_NAME}: ${SERVICE_URL}"
echo "Call POST with the X-API-Key header; GET ${SERVICE_URL} is the health check."
