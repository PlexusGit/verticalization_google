"""Functions Framework HTTP entry point for production vertical renders."""

from __future__ import annotations

import dataclasses
import hmac
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import functions_framework
from google.cloud import firestore, storage

from face_detection import FOCAL_STRATEGY_VERSION, analyze_focal_points
from renderer import RenderSpec, ValidationError, compact_keyframes, render, validate_request, write_ass_captions

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
DEFAULT_BUCKET = os.getenv("BUCKET_NAME", "")
VIDEOS_COLLECTION = os.getenv("COLLECTION_VIDEOS", "vertical_cuts_videos")
API_KEY = os.getenv("API_KEY") or os.getenv("SECURITY_TOKEN", "")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_storage_client: storage.Client | None = None
_firestore_client: firestore.Client | None = None


def storage_client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client(project=PROJECT_ID) if PROJECT_ID else storage.Client()
    return _storage_client


def firestore_client() -> firestore.Client:
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.Client(project=PROJECT_ID) if PROJECT_ID else firestore.Client()
    return _firestore_client


def split_gcs_path(path: str) -> tuple[str, str]:
    """Accept gs://bucket/object or an object path in the configured bucket."""
    if path.startswith("gs://"):
        parsed = urlparse(path)
        bucket, blob = parsed.netloc, parsed.path.lstrip("/")
    else:
        bucket, blob = DEFAULT_BUCKET, path.lstrip("/")
    if not bucket or not blob or ".." in blob.split("/"):
        raise ValidationError("GCS path must identify a bucket and object")
    return bucket, blob


def _safe_id(payload: dict[str, Any], field: str, required: bool = True) -> str | None:
    value = payload.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError(f"{field} must contain only letters, numbers, underscore, or hyphen")
    return value


def update_status(video_id: str, render_id: str, chapter_id: str | None, status: str, **fields: Any) -> None:
    """Best-effort status writes; a status outage must not hide the render result.

    Render state lives at videos/{video}/renders/{render}.  When chapter_id is
    provided, the corresponding chapter receives the same lifecycle status and
    output/error fields so both list and detail views remain consistent.
    """
    payload = {"status": status, "updated_at": firestore.SERVER_TIMESTAMP, **fields}
    try:
        db = firestore_client()
        refs = [db.collection(VIDEOS_COLLECTION).document(video_id).collection("renders").document(render_id)]
        if chapter_id:
            refs.append(db.collection(VIDEOS_COLLECTION).document(video_id).collection("chapters").document(chapter_id))
        batch = db.batch()
        for ref in refs:
            batch.set(ref, payload, merge=True)
        batch.commit()
    except Exception:
        logger.exception("Unable to update Firestore status for render %s", render_id)


def _authorized(request: Any) -> bool:
    supplied = request.headers.get("X-API-Key", "")
    # A missing server secret is a deployment error, never an auth bypass.
    return bool(API_KEY and supplied and hmac.compare_digest(supplied, API_KEY))


def _cors_headers() -> dict[str, str]:
    return {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}


def _download(gcs_path: str, destination: str) -> None:
    bucket, blob = split_gcs_path(gcs_path)
    storage_client().bucket(bucket).blob(blob).download_to_filename(destination)


def _upload(gcs_path: str, source: str) -> str:
    bucket, blob = split_gcs_path(gcs_path)
    storage_client().bucket(bucket).blob(blob).upload_from_filename(source, content_type="video/mp4", timeout=600)
    return f"gs://{bucket}/{blob}"


def _add_automatic_focus(spec: RenderSpec, source_path: str) -> RenderSpec:
    if spec.mode != "center_crop":
        return spec
    if spec.automatic_focal_points:
        return dataclasses.replace(spec, automatic_focal_points=compact_keyframes(spec.automatic_focal_points))
    try:
        points = analyze_focal_points(
            source_path,
            spec.trim_start,
            spec.trim_end,
            spec.source_width,
            spec.source_height,
        )
        return dataclasses.replace(spec, automatic_focal_points=compact_keyframes(points))
    except Exception:
        logger.exception("A análise facial falhou; usando recorte central")
        return spec


@functions_framework.http
def vertical_render(request: Any):
    """Authenticate, fetch assets, render, upload, update status, and clean up."""
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
            "Access-Control-Max-Age": "3600",
        })
    if request.method == "GET" and request.path.rstrip("/") in {"", "/health"}:
        return ({"status": "healthy", "service": "vertical-render"}, 200, _cors_headers())
    if request.method != "POST":
        return ({"error": "method not allowed"}, 405, _cors_headers())
    if not _authorized(request):
        return ({"error": "unauthorized"}, 401, _cors_headers())

    payload = request.get_json(silent=True)
    try:
        spec = validate_request(payload)
        assert isinstance(payload, dict)  # established by validate_request
        video_id = _safe_id(payload, "video_id")
        render_id = _safe_id(payload, "render_id")
        chapter_id = _safe_id(payload, "chapter_id", required=False)
    except ValidationError as exc:
        return ({"error": str(exc)}, 400, _cors_headers())

    update_status(video_id, render_id, chapter_id, "PROCESSING", started_at=firestore.SERVER_TIMESTAMP, error=firestore.DELETE_FIELD)
    logger.info(
        "Render recebido video=%s chapter=%s render=%s trecho=%.1fs–%.1fs modo=%s",
        video_id,
        chapter_id or "-",
        render_id,
        spec.trim_start,
        spec.trim_end,
        spec.mode,
    )
    try:
        # TemporaryDirectory guarantees removal on success, exceptions, and client
        # disconnects. Randomized names avoid collisions under Cloud Run concurrency.
        with tempfile.TemporaryDirectory(prefix="vertical-render-") as workdir:
            source_path = str(Path(workdir) / "source")
            output_path = str(Path(workdir) / "output.mp4")
            ass_path = str(Path(workdir) / "captions.ass") if spec.captions else None
            logo_path = str(Path(workdir) / "logo") if spec.logo else None
            _download(spec.source_gcs_path, source_path)
            spec = _add_automatic_focus(spec, source_path)
            if ass_path:
                write_ass_captions(spec, ass_path)
            if logo_path and spec.logo:
                _download(str(spec.logo["gcs_path"]), logo_path)
            render(spec, source_path, output_path, ass_path=ass_path, logo_path=logo_path)
            output_uri = _upload(spec.output_gcs_path, output_path)

        update_status(video_id, render_id, chapter_id, "COMPLETED", output_gcs_path=output_uri, completed_at=firestore.SERVER_TIMESTAMP, error=firestore.DELETE_FIELD)
        return ({"status": "COMPLETED", "video_id": video_id, "render_id": render_id, "chapter_id": chapter_id, "output_gcs_path": output_uri, "width": 1080, "height": 1920, "automatic_focal_points": list(spec.automatic_focal_points), "focal_strategy_version": FOCAL_STRATEGY_VERSION}, 200, _cors_headers())
    except ValidationError as exc:
        # Some style values are validated while assembling the filter graph.
        update_status(video_id, render_id, chapter_id, "ERROR", error=str(exc))
        return ({"error": str(exc)}, 400, _cors_headers())
    except Exception as exc:
        logger.exception("Vertical render failed")
        public_error = "render failed"
        update_status(video_id, render_id, chapter_id, "ERROR", error=str(exc)[:1500])
        return ({"error": public_error, "render_id": render_id}, 500, _cors_headers())
