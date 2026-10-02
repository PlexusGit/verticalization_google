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
USE_GPU="${TRANSCODE_USE_GPU:-0}"
AUTOFRAME_ENGINE="${AUTOFRAME_ENGINE:-legacy}"
[[ -n "${API_KEY_SECRET}" ]] || { echo "SECURITY_TOKEN_SECRET must be set" >&2; exit 1; }
[[ -n "${SERVICE_ACCOUNT}" ]] || { echo "VERTICAL_RENDER_SERVICE_ACCOUNT must be set" >&2; exit 1; }

# GPU builds install PyPI's CUDA-bundled torch wheels; CPU builds use the
# cpu-only wheel index to keep the image small.
BUILD_ARGS=()
if [[ "${USE_GPU}" == "1" ]]; then
  BUILD_ARGS+=(--build-arg "TORCH_INDEX=https://pypi.org/simple")
fi

printf 'Deploying %s\nImage: %s\nAutoframe engine: %s (GPU=%s)\n' "${SERVICE}" "${IMAGE}" "${AUTOFRAME_ENGINE}" "${USE_GPU}"
gcloud config set project "${PROJECT_ID}"
if ! gcloud artifacts repositories describe "${REPOSITORY}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" --repository-format=docker --location="${REGION}" --description="GNI render workers"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build --platform linux/amd64 ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -t "${IMAGE}" "${SCRIPT_DIR}"
docker push "${IMAGE}"

# Cloud Run GPU (NVIDIA L4) requires gen2, at least 4 CPUs, and 16 GiB.
GPU_ARGS=()
MEMORY="8Gi"
if [[ "${USE_GPU}" == "1" ]]; then
  GPU_ARGS=(--gpu=1 --gpu-type=nvidia-l4 --no-gpu-zonal-redundancy)
  MEMORY="16Gi"
else
  # --gpu=0 is required: Cloud Run deploy flags are sticky, so omitting --gpu
  # would keep a GPU allocated by an earlier revision.
  GPU_ARGS=(--gpu=0)
fi

gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" --region="${REGION}" --platform=managed \
  --execution-environment=gen2 --allow-unauthenticated \
  --service-account="${SERVICE_ACCOUNT}" \
  "${GPU_ARGS[@]+"${GPU_ARGS[@]}"}" \
  --cpu=4 --memory="${MEMORY}" --timeout=3600 --concurrency=1 \
  --min-instances=0 --max-instances="${VERTICAL_RENDER_MAX_INSTANCES:-5}" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},BUCKET_NAME=${BUCKET},COLLECTION_VIDEOS=${COLLECTION},AUTOFRAME_ENGINE=${AUTOFRAME_ENGINE},TRANSCODE_USE_GPU=${USE_GPU}" \
  --set-secrets="API_KEY=${API_KEY_SECRET}:latest"

gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)'
