"""Google Identity verification and Firestore allowlist helpers."""
from __future__ import annotations

from fastapi import Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google.cloud.firestore_v1.base_query import FieldFilter

from app.config import settings


def get_current_user(request: Request) -> dict | None:
    """Return the compact user stored in the signed session cookie."""

    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return {"id": user_id, "info": request.session.get("user_info", {})}


def verify_google_id_token(credential: str) -> dict | None:
    """Verify a Google Identity Services credential for this OAuth client."""

    try:
        return id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except Exception as exc:
        print(f"[auth] Google token verification failed: {type(exc).__name__}", flush=True)
        return None


def is_user_allowed(db, email: str) -> bool:
    """Check the allowlist using normalized email as the preferred document ID."""

    normalized = (email or "").strip().lower()
    if not normalized:
        return False
    collection = db.collection(settings.ALLOWED_EMAILS_COLLECTION)
    if collection.document(normalized).get().exists:
        return True
    return bool(list(collection.where(filter=FieldFilter("email", "==", normalized)).limit(1).stream()))


def request_is_authenticated(request: Request) -> bool:
    """Authorize browser sessions or the shared headless/importer API token."""

    if get_current_user(request):
        return True
    configured = settings.SECURITY_TOKEN
    supplied = request.headers.get("X-API-Key", "")
    return bool(configured and supplied and supplied == configured)
