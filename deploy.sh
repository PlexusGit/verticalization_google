#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
load_env
require_tools gcloud docker
require_vars GOOGLE_CLOUD_PROJECT BUCKET_NAME SERVICE_ACCOUNT_EMAIL GOOGLE_CLIENT_ID ARTIFACT_REGISTRY_REPOSITORY SECURITY_TOKEN_SECRET SECRET_KEY_SECRET GEMINI_API_KEY_SECRET

REGION="${DEPLOY_REGION:-us-central1}"
SERVICE="${SERVICE_NAME:-plexus-vertical-cuts}"
IMAGE="${REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/${ARTIFACT_REGISTRY_REPOSITORY}/${SERVICE}:${IMAGE_TAG:-latest}"
gcloud config set project "${GOOGLE_CLOUD_PROJECT}" >/dev/null
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "Construindo ${IMAGE}"
docker build --platform linux/amd64 -t "${IMAGE}" "${SCRIPT_DIR}"
docker push "${IMAGE}"

ENV_VARS="GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT},BUCKET_NAME=${BUCKET_NAME},FIRESTORE_DB_NAME=${FIRESTORE_DB_NAME:-(default)},COLLECTION_VIDEOS=${COLLECTION_VIDEOS:-vertical_cuts_videos},COLLECTION_USERS=${COLLECTION_USERS:-vertical_cuts_users},ALLOWED_EMAILS_COLLECTION=${ALLOWED_EMAILS_COLLECTION:-vertical_cuts_allowed_emails},COLLECTION_PROMPTS=${COLLECTION_PROMPTS:-vertical_cuts_prompts},COLLECTION_TEMPLATES=${COLLECTION_TEMPLATES:-vertical_cuts_templates},GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},TRANSCODE_URL=${TRANSCODE_URL:-},VERTICAL_RENDER_URL=${VERTICAL_RENDER_URL:-},GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.5-flash},CHIRP_LOCATION=${CHIRP_LOCATION:-us},CHIRP_FALLBACK_LOCATION=${CHIRP_FALLBACK_LOCATION:-eu},MAX_VIDEO_DURATION_SECONDS=${MAX_VIDEO_DURATION_SECONDS:-3600},DAILY_VIDEO_LIMIT=${DAILY_VIDEO_LIMIT:-20},DEFAULT_CHAPTER_MIN_SECONDS=${DEFAULT_CHAPTER_MIN_SECONDS:-15},DEFAULT_CHAPTER_MAX_SECONDS=${DEFAULT_CHAPTER_MAX_SECONDS:-180},OUTPUT_WIDTH=${OUTPUT_WIDTH:-1080},OUTPUT_HEIGHT=${OUTPUT_HEIGHT:-1920}"
DOMAIN_VALUE="${CLOUD_RUN_DOMAIN:-${DOMAIN:-http://localhost:8080}}"
ENV_VARS="${ENV_VARS},DOMAIN=${DOMAIN_VALUE}"

gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" --project "${GOOGLE_CLOUD_PROJECT}" --region "${REGION}" \
  --platform managed --allow-unauthenticated --service-account "${SERVICE_ACCOUNT_EMAIL}" \
  --cpu 2 --memory 4Gi --timeout 3600 --concurrency 20 --min-instances 0 --max-instances 10 --no-cpu-throttling \
  --set-env-vars "${ENV_VARS}" \
  --set-secrets "SECURITY_TOKEN=${SECURITY_TOKEN_SECRET}:latest,SECRET_KEY=${SECRET_KEY_SECRET}:latest,GEMINI_API_KEY=${GEMINI_API_KEY_SECRET}:latest"

URL="$(service_url "${SERVICE}" "${REGION}")"
echo "${URL}"
