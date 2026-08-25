"""Seed normalized login allowlist documents during GCP bootstrap."""
from __future__ import annotations

import os
from google.cloud import firestore


def normalize_adc_environment() -> None:
    """Let Google Auth use ADC when the optional JSON-key path is blank.

    ``google.auth.default`` treats the presence of an empty
    GOOGLE_APPLICATION_CREDENTIALS variable as an explicit path and raises a
    misleading "File was not found" error instead of checking local ADC.
    """

    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def seed_allowed_emails() -> int:
    """Write configured allowlist entries and return the number written."""

    normalize_adc_environment()
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    database = os.getenv("FIRESTORE_DB_NAME", "(default)")
    collection_name = os.getenv("ALLOWED_EMAILS_COLLECTION", "vertical_cuts_allowed_emails")
    emails = {
        item.strip().lower()
        for item in os.getenv("INITIAL_ALLOWED_EMAILS", "").split(",")
        if item.strip()
    }
    if not emails:
        print("Nenhum INITIAL_ALLOWED_EMAILS configurado; allowlist não alterada.")
        return 0

    db = firestore.Client(project=project, database=database)
    batch = db.batch()
    for email in sorted(emails):
        batch.set(db.collection(collection_name).document(email), {"email": email})
    batch.commit()
    print(f"Allowlist atualizada com {len(emails)} e-mail(s).")
    return len(emails)


if __name__ == "__main__":
    seed_allowed_emails()
