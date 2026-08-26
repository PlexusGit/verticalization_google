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
    info = dict(request.session.get("user_info", {}))
    info.setdefault("role", "admin")
    return {"id": user_id, "info": info}


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


def get_allowed_user(db, email: str) -> dict | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    collection = db.collection(settings.ALLOWED_EMAILS_COLLECTION)
    snapshot = collection.document(normalized).get()
    if not snapshot.exists:
        matches = list(collection.where(filter=FieldFilter("email", "==", normalized)).limit(1).stream())
        if not matches:
            return None
        snapshot = matches[0]
    data = (snapshot.to_dict() or {}) if hasattr(snapshot, "to_dict") else {}
    return {**data, "email": normalized, "role": data.get("role") or "admin"}


def is_user_allowed(db, email: str) -> bool:
    """Check the allowlist using normalized email as the preferred document ID."""

    return get_allowed_user(db, email) is not None


def request_is_admin(request: Request) -> bool:
    user = get_current_user(request)
    return bool(user and user["info"].get("role") == "admin")


def request_is_authenticated(request: Request) -> bool:
    """Authorize browser sessions or the shared headless/importer API token."""

    if get_current_user(request):
        return True
    configured = settings.SECURITY_TOKEN
    supplied = request.headers.get("X-API-Key", "")
    return bool(configured and supplied and supplied == configured)
