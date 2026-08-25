#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
python3 -m compileall -q app tests scripts/seed_allowed_emails.py
python3 -m pytest -q tests
