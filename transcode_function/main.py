"""HTTP entry point for the Plexus Vertical Cuts transcode worker."""

from __future__ import annotations

import hmac
import logging
import os
import threading
from typing import Any

import functions_framework
from flask import jsonify

from processor import Settings, TranscodeError, TranscodeProcessor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger(__name__)
_PROCESSOR: TranscodeProcessor | None = None
_PROCESSOR_LOCK = threading.Lock()
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": os.getenv("CORS_ALLOW_ORIGIN", "*"),
    "Vary": "Origin",
}


def get_processor() -> TranscodeProcessor:
    """Lazily initialize clients so health checks never require GCP credentials."""

    global _PROCESSOR
    if _PROCESSOR is None:
        with _PROCESSOR_LOCK:
            if _PROCESSOR is None:
                _PROCESSOR = TranscodeProcessor(Settings.from_env())
    return _PROCESSOR


def _response(payload: dict[str, Any], status: int):
    response = jsonify(payload)
    response.status_code = status
    response.headers.update(_CORS_HEADERS)
    return response


def _authorized(request) -> bool:
    configured = os.getenv("SECURITY_TOKEN", "")
    supplied = request.headers.get("X-API-Key", "")
    # A missing server-side key is a configuration error, not an auth bypass.
    return bool(configured and supplied) and hmac.compare_digest(configured, supplied)


@functions_framework.http
def process_transcode(request):
    """Handle health checks, CORS preflight, and authenticated transcode jobs."""

    if request.method == "OPTIONS":
        response = _response({}, 204)
        response.headers.update({
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
            "Access-Control-Max-Age": "3600",
        })
        return response

    if request.method == "GET":
        return _response({"status": "healthy", "service": "plexus-vertical-cuts-transcode"}, 200)

    if request.method != "POST":
        response = _response({"error": "method_not_allowed"}, 405)
        response.headers["Allow"] = "GET, POST, OPTIONS"
        return response

    if not _authorized(request):
        return _response({"error": "unauthorized"}, 401)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _response({"error": "request body must be a JSON object"}, 400)

    missing = [name for name in ("video_id", "gcs_source_path") if not payload.get(name)]
    if missing:
        return _response({"error": f"missing required field(s): {', '.join(missing)}"}, 400)

    try:
        result = get_processor().process(
            video_id=payload["video_id"],
            source_path=payload["gcs_source_path"],
            high_path=payload.get("high_gcs_path"),
            audio_prefix=payload.get("audio_gcs_prefix"),
        )
        return _response(result, 200)
    except (ValueError, FileNotFoundError) as exc:
        return _response({"error": str(exc)}, 400 if isinstance(exc, ValueError) else 404)
    except TranscodeError as exc:
        LOGGER.exception("Transcode job failed for video_id=%r", payload.get("video_id"))
        return _response({"error": str(exc)}, 422)
    except Exception:
        # Do not leak credentials, local paths, or SDK details to callers.
        LOGGER.exception("Unexpected transcode failure for video_id=%r", payload.get("video_id"))
        return _response({"error": "internal transcode error"}, 500)
