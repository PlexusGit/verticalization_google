"""Lazy Firestore client construction for local ADC and Cloud Run identity."""
from __future__ import annotations

import os
from google.cloud import firestore

from app.config import settings

_client: firestore.Client | None = None


def get_db() -> firestore.Client:
    """Return the process-wide Firestore client.

    Cloud Run uses its attached service account. Local development may point to
    an ignored JSON key, although Application Default Credentials are preferred.
    Initialization errors are raised so API callers receive an honest failure.
    """

    global _client
    if _client is not None:
        return _client

    kwargs: dict = {"project": settings.PROJECT_ID, "database": settings.FIRESTORE_DB_NAME}
    credential_path = settings.GOOGLE_APPLICATION_CREDENTIALS
    if credential_path and os.path.isfile(credential_path):
        from google.oauth2 import service_account

        kwargs["credentials"] = service_account.Credentials.from_service_account_file(credential_path)
    _client = firestore.Client(**kwargs)
    return _client
