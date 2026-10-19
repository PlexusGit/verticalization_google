"""Import finalized GCS objects into the Plexus Vertical Cuts API.

This Google Cloud Functions Gen2 entry point consumes a Pub/Sub CloudEvent. The
Pub/Sub message data must be the JSON payload produced by a standard Cloud
Storage object-finalize notification. Irrelevant/malformed messages are
acknowledged, while network and upstream service errors are raised so Pub/Sub
can retry them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import PurePosixPath
from typing import Any, Mapping

import functions_framework
import requests

LOGGER = logging.getLogger(__name__)
DEFAULT_EXTENSIONS = ".mp4,.mov,.mkv,.avi,.webm,.m4v,.mpg,.mpeg"


def _configured_extensions() -> set[str]:
    """Return normalized extensions from the runtime configuration."""
    raw = os.getenv("GCS_ALLOWED_EXTENSIONS", os.getenv("ALLOWED_EXTENSIONS", DEFAULT_EXTENSIONS))
    return {
        extension if extension.startswith(".") else f".{extension}"
        for item in raw.split(",")
        if (extension := item.strip().lower())
    }


def _decode_pubsub_message(cloud_event: Any) -> tuple[dict[str, Any] | None, Mapping[str, Any]]:
    """Decode a Pub/Sub envelope without throwing on a poison message."""
    event_data = getattr(cloud_event, "data", None) or {}
    message = event_data.get("message") if isinstance(event_data, Mapping) else None
    if not isinstance(message, Mapping):
        LOGGER.warning("Ignoring event without a Pub/Sub message")
        return None, {}

    encoded = message.get("data")
    if not isinstance(encoded, str):
        LOGGER.warning("Ignoring Pub/Sub message without base64 data")
        return None, message.get("attributes") or {}

    try:
        payload = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        LOGGER.warning("Ignoring Pub/Sub message with invalid encoded JSON")
        return None, message.get("attributes") or {}

    if not isinstance(payload, dict):
        LOGGER.warning("Ignoring Pub/Sub message whose payload is not an object")
        return None, message.get("attributes") or {}
    return payload, message.get("attributes") or {}


def _idempotency_key(bucket: str, object_name: str, generation: str) -> str:
    """Create a stable identity for one immutable GCS object generation."""
    identity = f"gcs\0{bucket}\0{object_name}\0{generation}".encode("utf-8")
    return "gcs-" + hashlib.sha256(identity).hexdigest()


def _build_api_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build deterministic metadata, excluding delivery-specific Pub/Sub data."""
    bucket = str(payload["bucket"])
    object_name = str(payload["name"])
    generation = str(payload.get("generation") or "")
    custom_metadata = payload.get("metadata")
    if not isinstance(custom_metadata, Mapping):
        custom_metadata = {}
    metadata = {
        "provider": "gcs",
        "bucket": bucket,
        "key": object_name,
        "generation": generation,
        "metageneration": str(payload.get("metageneration") or ""),
        "etag": str(payload.get("etag") or ""),
        "size": str(payload.get("size") or ""),
        "content_type": str(payload.get("contentType") or ""),
        "md5_hash": str(payload.get("md5Hash") or ""),
        "crc32c": str(payload.get("crc32c") or ""),
        "created_at": str(payload.get("timeCreated") or ""),
        "updated_at": str(payload.get("updated") or ""),
        "custom_metadata": dict(sorted(custom_metadata.items())),
    }
    key = _idempotency_key(bucket, object_name, generation)
    return {
        "source": "gcs",
        "bucket": bucket,
        "object_name": object_name,
        "gcs_uri": f"gs://{bucket}/{object_name}",
        "source_metadata": metadata,
        "idempotency_key": key,
    }


def _post_import(api_payload: Mapping[str, Any]) -> None:
    """Notify the main API, raising operational failures for Pub/Sub retry."""
    main_api_url = os.getenv("MAIN_API_URL", "").strip()
    api_token = os.getenv("API_TOKEN", "").strip()
    if not main_api_url or not api_token:
        raise RuntimeError("MAIN_API_URL and API_TOKEN must be configured")

    url = f"{main_api_url.rstrip('/')}/api/import/gcs"
    headers = {
        "X-API-Key": api_token,
        "Idempotency-Key": str(api_payload["idempotency_key"]),
        "Content-Type": "application/json",
    }
    # Do not log headers: X-API-Key is supplied through Secret Manager.
    try:
        response = requests.post(url, json=dict(api_payload), headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError("Main API request failed") from exc

    # A duplicate response means a previous delivery already completed.
    if response.status_code == 409:
        LOGGER.info("Main API reports import already exists")
        return
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Main API returned HTTP {response.status_code}")


@functions_framework.cloud_event
def process_gcs_bucket(cloud_event: Any) -> None:
    """Handle one Pub/Sub delivery containing a GCS object-finalize payload."""
    payload, attributes = _decode_pubsub_message(cloud_event)
    if payload is None:
        return

    event_type = str(attributes.get("eventType") or attributes.get("event_type") or "")
    if event_type and event_type not in {"OBJECT_FINALIZE", "google.cloud.storage.object.v1.finalized"}:
        LOGGER.info("Ignoring non-finalize Cloud Storage event")
        return

    bucket = payload.get("bucket")
    object_name = payload.get("name")
    if not isinstance(bucket, str) or not isinstance(object_name, str) or not object_name:
        LOGGER.warning("Ignoring GCS payload missing bucket or object name")
        return

    watch_bucket = os.getenv("GCS_WATCH_BUCKET", os.getenv("WATCH_BUCKET", "")).strip()
    if not watch_bucket:
        raise RuntimeError("GCS_WATCH_BUCKET must be configured")
    if bucket != watch_bucket:
        LOGGER.info("Ignoring object from an unwatched bucket")
        return

    if PurePosixPath(object_name).suffix.lower() not in _configured_extensions():
        LOGGER.info("Ignoring object with an unsupported extension")
        return

    api_payload = _build_api_payload(payload)
    LOGGER.info(
        "Importing finalized GCS object bucket=%s object_sha256=%s",
        bucket,
        hashlib.sha256(object_name.encode("utf-8")).hexdigest()[:16],
    )
    _post_import(api_payload)
