"""Copy S3 object-created events to GCS and register them with the main API.

The Gen2 Cloud Function accepts Pub/Sub CloudEvents whose decoded JSON is either
a canonical object (``bucket``/``key``) or an AWS notification containing
``Records[]``. S3 keys are URL-decoded, downloaded to temporary storage, and
uploaded to a stable collision-safe GCS path before the API is notified.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
from pathlib import PurePosixPath
from typing import Any, Iterator, Mapping
from urllib.parse import unquote_plus

import boto3
import functions_framework
import requests
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

LOGGER = logging.getLogger(__name__)
DEFAULT_EXTENSIONS = ".mp4,.mov,.mkv,.webm,.avi,.mxf"
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024
# Client errors that no retry can fix (bad payload/format). Auth, throttling and
# 5xx responses stay retryable because an operator or a later attempt can fix them.
PERMANENT_REJECTIONS = {400, 404, 413, 415, 422}


def _configured_extensions() -> set[str]:
    raw = os.getenv("S3_ALLOWED_EXTENSIONS", os.getenv("ALLOWED_EXTENSIONS", DEFAULT_EXTENSIONS))
    return {
        extension if extension.startswith(".") else f".{extension}"
        for item in raw.split(",")
        if (extension := item.strip().lower())
    }


def _decode_pubsub_message(cloud_event: Any) -> dict[str, Any] | None:
    """Decode Pub/Sub JSON; malformed poison messages are acknowledged."""
    event_data = getattr(cloud_event, "data", None) or {}
    message = event_data.get("message") if isinstance(event_data, Mapping) else None
    encoded = message.get("data") if isinstance(message, Mapping) else None
    if not isinstance(encoded, str):
        LOGGER.warning("Ignoring event without Pub/Sub base64 data")
        return None
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        LOGGER.warning("Ignoring Pub/Sub message with invalid encoded JSON")
        return None
    if not isinstance(payload, dict):
        LOGGER.warning("Ignoring Pub/Sub payload that is not an object")
        return None
    return payload


def _object_from_canonical(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize the intentionally small canonical publisher format."""
    bucket = payload.get("bucket") or payload.get("bucket_name")
    key = payload.get("key") or payload.get("object_key")
    if not isinstance(bucket, str) or not isinstance(key, str):
        return None
    return {
        "bucket": bucket,
        "key": unquote_plus(key),
        "version_id": payload.get("versionId") or payload.get("version_id"),
        "etag": payload.get("eTag") or payload.get("etag"),
        "size": payload.get("size"),
        "sequencer": payload.get("sequencer"),
        "event_name": payload.get("eventName") or payload.get("event_name") or "ObjectCreated",
        "event_time": payload.get("eventTime") or payload.get("event_time"),
        "aws_region": payload.get("awsRegion") or payload.get("aws_region"),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {},
    }


def _iter_s3_objects(payload: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield normalized objects from canonical or AWS Records[] payloads."""
    records = payload.get("Records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, Mapping):
                continue
            event_name = str(record.get("eventName") or "")
            # Removal/test events are irrelevant and must not be retried.
            if event_name and not event_name.startswith("ObjectCreated:"):
                continue
            s3 = record.get("s3")
            bucket_data = s3.get("bucket") if isinstance(s3, Mapping) else None
            object_data = s3.get("object") if isinstance(s3, Mapping) else None
            bucket = bucket_data.get("name") if isinstance(bucket_data, Mapping) else None
            key = object_data.get("key") if isinstance(object_data, Mapping) else None
            if not isinstance(bucket, str) or not isinstance(key, str):
                continue
            yield {
                "bucket": bucket,
                "key": unquote_plus(key),
                "version_id": object_data.get("versionId"),
                "etag": object_data.get("eTag"),
                "size": object_data.get("size"),
                "sequencer": object_data.get("sequencer"),
                "event_name": event_name,
                "event_time": record.get("eventTime"),
                "aws_region": record.get("awsRegion"),
                "metadata": (
                    object_data.get("metadata")
                    if isinstance(object_data.get("metadata"), Mapping)
                    else {}
                ),
            }
        return

    canonical = _object_from_canonical(payload)
    if canonical is not None:
        event_name = str(canonical.get("event_name") or "")
        if not event_name or event_name.startswith("ObjectCreated"):
            yield canonical


def _idempotency_key(source: Mapping[str, Any]) -> str:
    """Use immutable source identity where available and stable fallbacks."""
    version = source.get("version_id") or source.get("etag") or source.get("sequencer") or ""
    raw = f"s3\0{source['bucket']}\0{source['key']}\0{version}".encode("utf-8")
    return "s3-" + hashlib.sha256(raw).hexdigest()


def _destination_path(source: Mapping[str, Any]) -> str:
    """Create a deterministic path that cannot collide across S3 objects."""
    prefix = os.getenv("S3_GCS_PREFIX", "imports/s3").strip("/") or "imports/s3"
    safe_bucket = _SAFE_COMPONENT.sub("_", str(source["bucket"]))
    filename = PurePosixPath(str(source["key"])).name or "object"
    safe_filename = _SAFE_COMPONENT.sub("_", filename)[:180] or "object"
    digest = _idempotency_key(source).removeprefix("s3-")
    return f"{prefix}/{safe_bucket}/{digest}/{safe_filename}"


def _main_bucket() -> str:
    main_bucket = os.getenv("GCS_MAIN_BUCKET", os.getenv("MAIN_BUCKET", "")).strip()
    if not main_bucket:
        raise RuntimeError("GCS_MAIN_BUCKET must be configured")
    return main_bucket


def _download_and_upload(source: Mapping[str, Any], destination: str) -> None:
    """Stream one S3 object into GCS without staging it in memory or on disk.

    Cloud Functions keep the file system in memory, so the object is piped from
    S3 to GCS in chunks. A resumable upload only creates the object when it is
    finalized, so a failed transfer never leaves a partial video behind.
    """
    blob = storage.Client().bucket(_main_bucket()).blob(destination)
    if blob.exists():
        # A previous delivery already copied this exact object version.
        LOGGER.info("Deterministic GCS destination already exists")
        return

    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION") or None)
    request = {"Bucket": str(source["bucket"]), "Key": str(source["key"])}
    if source.get("version_id"):
        request["VersionId"] = str(source["version_id"])
    response = s3.get_object(**request)
    body = response["Body"]
    try:
        blob.metadata = {
            "source-provider": "s3",
            "source-bucket": str(source["bucket"]),
            "source-key-sha256": hashlib.sha256(str(source["key"]).encode("utf-8")).hexdigest(),
            "source-version-id": str(source.get("version_id") or ""),
            "source-etag": str(source.get("etag") or ""),
            "idempotency-key": _idempotency_key(source),
        }
        blob.chunk_size = UPLOAD_CHUNK_BYTES
        content_type = mimetypes.guess_type(str(source["key"]))[0] or "application/octet-stream"
        try:
            # Generation zero makes concurrent/retried deliveries idempotent.
            blob.upload_from_file(
                body,
                content_type=content_type,
                size=response.get("ContentLength"),
                if_generation_match=0,
            )
        except PreconditionFailed:
            LOGGER.info("Deterministic GCS destination already exists")
    finally:
        body.close()


def _delete_intermediate_copy(destination: str) -> None:
    """Remove the staging copy once the API owns a managed copy (best effort)."""
    try:
        storage.Client().bucket(_main_bucket()).blob(destination).delete()
    except Exception:
        LOGGER.warning("Could not delete the intermediate S3 import copy", exc_info=True)


def _build_api_payload(source: Mapping[str, Any], destination: str) -> dict[str, Any]:
    main_bucket = _main_bucket()
    key = _idempotency_key(source)
    return {
        "source": "s3",
        "bucket": main_bucket,
        "object_name": destination,
        "gcs_uri": f"gs://{main_bucket}/{destination}",
        "source_metadata": {
            "provider": "s3",
            "bucket": str(source["bucket"]),
            "key": str(source["key"]),
            "version_id": str(source.get("version_id") or ""),
            "etag": str(source.get("etag") or ""),
            "size": str(source.get("size") or ""),
            "sequencer": str(source.get("sequencer") or ""),
            "event_name": str(source.get("event_name") or ""),
            "event_time": str(source.get("event_time") or ""),
            "aws_region": str(source.get("aws_region") or ""),
            "custom_metadata": dict(sorted((source.get("metadata") or {}).items())),
        },
        "idempotency_key": key,
    }


def _post_import(api_payload: Mapping[str, Any]) -> None:
    main_api_url = os.getenv("MAIN_API_URL", "").strip()
    api_token = os.getenv("API_TOKEN", "").strip()
    if not main_api_url or not api_token:
        raise RuntimeError("MAIN_API_URL and API_TOKEN must be configured")
    headers = {
        "X-API-Key": api_token,
        "Idempotency-Key": str(api_payload["idempotency_key"]),
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            f"{main_api_url.rstrip('/')}/api/import/gcs",
            json=dict(api_payload),
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Main API request failed") from exc
    if response.status_code == 409:
        LOGGER.info("Main API reports import already exists")
        return
    if response.status_code in PERMANENT_REJECTIONS:
        # Retrying cannot change the outcome and would loop forever.
        LOGGER.error("Main API permanently rejected the import (HTTP %s); dropping message", response.status_code)
        return
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Main API returned HTTP {response.status_code}")


@functions_framework.cloud_event
def process_s3_bucket(cloud_event: Any) -> None:
    """Copy each relevant S3 event object to GCS and invoke the import API."""
    payload = _decode_pubsub_message(cloud_event)
    if payload is None:
        return

    watch_bucket = os.getenv("S3_WATCH_BUCKET", os.getenv("WATCH_BUCKET", "")).strip()
    if not watch_bucket:
        raise RuntimeError("S3_WATCH_BUCKET must be configured")

    matched = False
    for source in _iter_s3_objects(payload):
        if source["bucket"] != watch_bucket:
            LOGGER.info("Ignoring object from an unwatched S3 bucket")
            continue
        if PurePosixPath(str(source["key"])).suffix.lower() not in _configured_extensions():
            LOGGER.info("Ignoring S3 object with an unsupported extension")
            continue

        matched = True
        destination = _destination_path(source)
        LOGGER.info(
            "Copying S3 object bucket=%s key_sha256=%s",
            source["bucket"],
            hashlib.sha256(str(source["key"]).encode("utf-8")).hexdigest()[:16],
        )
        _download_and_upload(source, destination)
        _post_import(_build_api_payload(source, destination))
        _delete_intermediate_copy(destination)

    if not matched:
        LOGGER.info("No relevant S3 objects in Pub/Sub message")
