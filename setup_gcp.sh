#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${ROOT}/scripts/lib.sh"
load_env
require_tools gcloud python3
require_vars GOOGLE_CLOUD_PROJECT DEPLOY_REGION BUCKET_NAME ARTIFACT_REGISTRY_REPOSITORY SERVICE_ACCOUNT_EMAIL SECURITY_TOKEN SECRET_KEY GEMINI_API_KEY
CHECK_ONLY=0
[[ "${1:-}" != "--check" ]] || CHECK_ONLY=1

PROJECT="${GOOGLE_CLOUD_PROJECT}"
REGION="${DEPLOY_REGION}"
SA_NAME="${SERVICE_ACCOUNT_NAME:-${SERVICE_ACCOUNT_EMAIL%%@*}}"

echo "Validando projeto existente ${PROJECT}…"
gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -n1 | grep -q . || { echo "Execute gcloud auth login." >&2; exit 1; }
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "Application Default Credentials ausentes. Execute: gcloud auth application-default login" >&2
  exit 1
fi
gcloud projects describe "${PROJECT}" >/dev/null
gcloud billing projects describe "${PROJECT}" --format='value(billingEnabled)' | grep -qi true || { echo "O projeto precisa ter faturamento ativo." >&2; exit 1; }
if [[ "${CHECK_ONLY}" == "1" ]]; then
  echo "Validação concluída sem alterações."
  echo "Projeto: ${PROJECT}; região: ${REGION}; bucket: ${BUCKET_NAME}; conta: ${SERVICE_ACCOUNT_EMAIL}"
  echo "Execute ./setup_gcp.sh sem --check para criar ou atualizar os recursos."
  exit 0
fi
gcloud config set project "${PROJECT}" >/dev/null

APIS=(run.googleapis.com cloudfunctions.googleapis.com eventarc.googleapis.com pubsub.googleapis.com firestore.googleapis.com storage.googleapis.com speech.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com iam.googleapis.com iamcredentials.googleapis.com secretmanager.googleapis.com serviceusage.googleapis.com)
echo "Habilitando APIs…"
gcloud services enable "${APIS[@]}" --project "${PROJECT}"

if ! gcloud iam service-accounts describe "${SERVICE_ACCOUNT_EMAIL}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" --project "${PROJECT}" --display-name "Plexus Vertical Cuts runtime"
fi

for role in roles/datastore.user roles/storage.objectAdmin roles/speech.client roles/secretmanager.secretAccessor roles/run.invoker roles/pubsub.publisher roles/pubsub.subscriber roles/eventarc.eventReceiver; do
  gcloud projects add-iam-policy-binding "${PROJECT}" --member "serviceAccount:${SERVICE_ACCOUNT_EMAIL}" --role "${role}" --quiet >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "${SERVICE_ACCOUNT_EMAIL}" --project "${PROJECT}" --member "serviceAccount:${SERVICE_ACCOUNT_EMAIL}" --role roles/iam.serviceAccountTokenCreator --quiet >/dev/null

if ! gcloud firestore databases describe --database "${FIRESTORE_DB_NAME:-(default)}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud firestore databases create --database "${FIRESTORE_DB_NAME:-(default)}" --location "${FIRESTORE_LOCATION:-nam5}" --type firestore-native --project "${PROJECT}"
fi
if ! gcloud artifacts repositories describe "${ARTIFACT_REGISTRY_REPOSITORY}" --location "${REGION}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${ARTIFACT_REGISTRY_REPOSITORY}" --repository-format docker --location "${REGION}" --project "${PROJECT}" --description "Plexus Vertical Cuts images"
fi
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" --project "${PROJECT}" --location "${REGION}" --uniform-bucket-level-access
fi
if [[ -n "${GCS_WATCH_BUCKET:-}" ]] && ! gcloud storage buckets describe "gs://${GCS_WATCH_BUCKET}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${GCS_WATCH_BUCKET}" --project "${PROJECT}" --location "${REGION}" --uniform-bucket-level-access
fi

for topic in "${GCS_PUBSUB_TOPIC:-plexus-vertical-cuts-gcs-imports}" "${S3_PUBSUB_TOPIC:-plexus-vertical-cuts-s3-imports}"; do
  gcloud pubsub topics describe "${topic}" --project "${PROJECT}" >/dev/null 2>&1 || gcloud pubsub topics create "${topic}" --project "${PROJECT}"
done
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:service-${PROJECT_NUMBER}@gs-project-accounts.iam.gserviceaccount.com" \
  --role roles/pubsub.publisher --quiet >/dev/null
if [[ -n "${GCS_WATCH_BUCKET:-}" ]]; then
  if ! gcloud storage buckets notifications list "gs://${GCS_WATCH_BUCKET}" --format='value(topic)' | grep -q "${GCS_PUBSUB_TOPIC}"; then
    gcloud storage buckets notifications create "gs://${GCS_WATCH_BUCKET}" --topic "${GCS_PUBSUB_TOPIC}" --event-types OBJECT_FINALIZE --payload-format json
  fi
fi

put_secret() {
  local name="$1" value="$2"
  gcloud secrets describe "${name}" --project "${PROJECT}" >/dev/null 2>&1 || gcloud secrets create "${name}" --replication-policy automatic --project "${PROJECT}"
  printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=- --project "${PROJECT}" >/dev/null
}
put_secret "${SECURITY_TOKEN_SECRET:-plexus-vertical-cuts-api-token}" "${SECURITY_TOKEN}"
put_secret "${SECRET_KEY_SECRET:-plexus-vertical-cuts-session-key}" "${SECRET_KEY}"
put_secret "${GEMINI_API_KEY_SECRET:-plexus-vertical-cuts-gemini-key}" "${GEMINI_API_KEY}"
if [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
  put_secret "${AWS_ACCESS_KEY_ID_SECRET:-plexus-vertical-cuts-aws-access-key}" "${AWS_ACCESS_KEY_ID}"
  put_secret "${AWS_SECRET_ACCESS_KEY_SECRET:-plexus-vertical-cuts-aws-secret-key}" "${AWS_SECRET_ACCESS_KEY}"
  [[ -z "${AWS_SESSION_TOKEN:-}" ]] || put_secret "${AWS_SESSION_TOKEN_SECRET:-plexus-vertical-cuts-aws-session-token}" "${AWS_SESSION_TOKEN}"
fi

CORS_FILE="$(mktemp)"; trap 'rm -f "${CORS_FILE}"' EXIT
CORS_ORIGIN="${CLOUD_RUN_DOMAIN:-${DOMAIN:-http://localhost:8080}}"
printf '[{"origin":["%s","http://localhost:8080"],"method":["GET","HEAD","PUT","POST"],"responseHeader":["Content-Type","Content-Length","Range"],"maxAgeSeconds":3600}]' "${CORS_ORIGIN}" > "${CORS_FILE}"
gcloud storage buckets update "gs://${BUCKET_NAME}" --cors-file "${CORS_FILE}" --project "${PROJECT}" >/dev/null

GOOGLE_CLOUD_PROJECT="${PROJECT}" python3 "${ROOT}/scripts/seed_allowed_emails.py"
echo "Configuração GCP concluída."
echo "Ações manuais: crie o cliente OAuth Web/consentimento, autorize localhost e a futura URL Cloud Run, e confirme quota NVIDIA L4 se TRANSCODE_USE_GPU=1."
echo "A integração AWS → Pub/Sub também deve ser configurada no ambiente AWS quando o importador S3 for usado."
