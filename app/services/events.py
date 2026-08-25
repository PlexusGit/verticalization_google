"""Structured runtime logs and durable Firestore history for each video."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from google.cloud import firestore

from app.config import settings
from app.db import get_db

logger = logging.getLogger("plexus.pipeline")


def _safe(value: Any) -> Any:
    """Convert details to bounded Firestore/JSON-safe values without secrets."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, dict):
        return {str(key)[:100]: _safe(item) for key, item in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in list(value)[:100]]
    return str(value)[:2000]


def record(
    video_id: str,
    stage: str,
    action: str,
    message: str,
    *,
    level: str = "INFO",
    details: dict[str, Any] | None = None,
) -> None:
    """Write one runtime log and best-effort durable event.

    Logging must never stop processing, so Firestore failures are reported to
    stderr but are not raised back into the media pipeline.
    """

    safe_details = _safe(details or {})
    payload = {
        "video_id": video_id,
        "stage": stage,
        "action": action,
        "message": message,
        "details": safe_details,
    }
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.log(numeric_level, json.dumps(payload, ensure_ascii=False, default=str))
    try:
        get_db().collection(settings.COLLECTION_VIDEOS).document(video_id).collection("events").document().set(
            {
                **payload,
                "level": logging.getLevelName(numeric_level),
                "created_at": firestore.SERVER_TIMESTAMP,
                "local_time": datetime.now(timezone.utc).isoformat(),
            }
        )
    except Exception as exc:
        logger.warning("Could not persist event video=%s action=%s: %s", video_id, action, exc)
