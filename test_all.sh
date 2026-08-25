#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${ROOT}/test.sh"
"${ROOT}/transcode_function/test.sh"
"${ROOT}/vertical_render_function/test.sh"
"${ROOT}/functions/process_gcs_bucket/test.sh"
"${ROOT}/functions/process_S3_bucket/test.sh"
for script in "${ROOT}"/*.sh "${ROOT}"/scripts/*.sh "${ROOT}"/transcode_function/*.sh "${ROOT}"/vertical_render_function/*.sh "${ROOT}"/functions/*/*.sh; do bash -n "${script}"; done
echo "Todos os testes e verificações de shell foram concluídos."
