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
from renderer import (
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    RenderSpec,
    ValidationError,
    build_overlay_filters,
    compact_keyframes,
    render,
    validate_request,
    write_ass_captions,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
DEFAULT_BUCKET = os.getenv("BUCKET_NAME", "")
VIDEOS_COLLECTION = os.getenv("COLLECTION_VIDEOS", "vertical_cuts_videos")
API_KEY = os.getenv("API_KEY") or os.getenv("SECURITY_TOKEN", "")
# "v10" enables the offline analyze->plan->render autoframe pipeline for
# center_crop requests; "legacy" keeps the YuNet focal-keyframe path as the
# immediate rollback option. Manual split_stack always uses the legacy path.
AUTOFRAME_ENGINE = os.getenv("AUTOFRAME_ENGINE", "legacy").strip().lower()
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


def _download(gcs_path: str, destination: str) -> str | None:
    """Download a GCS object; returns its generation for plan fingerprinting."""
    bucket, blob = split_gcs_path(gcs_path)
    obj = storage_client().bucket(bucket).get_blob(blob)
    if obj is None:
        raise RuntimeError(f"GCS object not found: {gcs_path}")
    obj.download_to_filename(destination)
    return None if obj.generation is None else str(obj.generation)


def _download_bytes(gcs_path: str) -> bytes:
    bucket, blob = split_gcs_path(gcs_path)
    return storage_client().bucket(bucket).blob(blob).download_as_bytes()


def _upload(gcs_path: str, source: str) -> str:
    bucket, blob = split_gcs_path(gcs_path)
    storage_client().bucket(bucket).blob(blob).upload_from_filename(source, content_type="video/mp4", timeout=600)
    return f"gs://{bucket}/{blob}"


def _upload_bytes(gcs_path: str, data: bytes, content_type: str) -> str:
    bucket, blob = split_gcs_path(gcs_path)
    storage_client().bucket(bucket).blob(blob).upload_from_string(data, content_type=content_type)
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


def _render_with_v10(
    spec: RenderSpec,
    source_path: str,
    output_path: str,
    workdir: str,
    *,
    source_generation: str | None,
    video_id: str,
    chapter_id: str | None,
    ass_path: str | None,
    logo_path: str | None,
) -> dict[str, Any]:
    """Analyze, plan, and render an automatic center_crop chapter with v10.

    Returns response extras (plan artifact location and stats).  Raises on any
    failure; the caller falls back to a deterministic central crop.
    """
    from autoframe_v10 import artifacts
    from autoframe_v10.analyze import analyze as v10_analyze
    from autoframe_v10.config import Config, resolve_device, resolve_encoder
    from autoframe_v10.plan import plan as v10_plan
    from autoframe_v10.render import render_plan

    device = resolve_device()
    cfg = Config(
        video=source_path,
        start=spec.trim_start,
        duration=spec.duration,
        workdir=workdir,
        output_w=OUTPUT_WIDTH,
        output_h=OUTPUT_HEIGHT,
        device=device,
        encoder=resolve_encoder(device),
    )
    logger.info("v10 autoframe device=%s encoder=%s", cfg.device, cfg.encoder)
    expected_fp = artifacts.plan_fingerprint(
        cfg,
        source_gcs_path=spec.source_gcs_path,
        source_generation=source_generation,
        trim_start=spec.trim_start,
        trim_end=spec.trim_end,
        source_width=spec.source_width,
        source_height=spec.source_height,
    )

    plan_doc = None
    if spec.camera_plan_gcs_path and spec.camera_plan_fingerprint:
        if spec.camera_plan_fingerprint == expected_fp:
            try:
                plan_doc = artifacts.loads_plan(_download_bytes(spec.camera_plan_gcs_path))
                if plan_doc.get("fingerprint") != expected_fp:
                    logger.warning("v10 plan fingerprint interno diverge; re-analisando")
                    plan_doc = None
                else:
                    logger.info("v10 reutilizando plano do rascunho %s", spec.camera_plan_gcs_path)
            except Exception:
                logger.exception("v10 plano reutilizável indisponível; re-analisando")
                plan_doc = None
        else:
            logger.info(
                "v10 fingerprint diverge do rascunho (%s… != %s…); re-analisando",
                spec.camera_plan_fingerprint[:12],
                expected_fp[:12],
            )

    artifact_uri = ""
    analysis = None
    if plan_doc is None:
        analysis = v10_analyze(cfg)
        plan_doc = v10_plan(cfg, analysis)
        plan_doc["format"] = artifacts.PLAN_FORMAT
        plan_doc["fingerprint"] = expected_fp
        out_bucket, _ = split_gcs_path(spec.output_gcs_path)
        blob_path = artifacts.artifact_blob_path(video_id, chapter_id or "chapter", expected_fp)
        artifact_uri = _upload_bytes(f"gs://{out_bucket}/{blob_path}", artifacts.dumps_plan(plan_doc), "application/gzip")
        logger.info("v10 plano publicado em %s", artifact_uri)

    def overlay_builder(input_label: str, logo_index: int | None):
        return build_overlay_filters(spec, input_label, ass_path, logo_index, cfg.work)

    stats = render_plan(
        cfg,
        plan_doc,
        output_path,
        overlay_builder=overlay_builder,
        logo_path=logo_path,
        analysis=analysis,
    )
    return {
        "camera_plan_gcs_path": artifact_uri or spec.camera_plan_gcs_path,
        "camera_plan_fingerprint": expected_fp,
        "autoframe_stats": {
            "device": cfg.device,
            "encoder": stats["encoder"],
            "frames": stats["frames"],
            "segments": len(plan_doc.get("segments", [])),
            "plan_reused": analysis is None,
        },
    }


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
        "Render recebido video=%s chapter=%s render=%s trecho=%.1fs–%.1fs modo=%s engine=%s",
        video_id,
        chapter_id or "-",
        render_id,
        spec.trim_start,
        spec.trim_end,
        spec.mode,
        AUTOFRAME_ENGINE,
    )
    try:
        # TemporaryDirectory guarantees removal on success, exceptions, and client
        # disconnects. Randomized names avoid collisions under Cloud Run concurrency.
        with tempfile.TemporaryDirectory(prefix="vertical-render-") as workdir:
            source_path = str(Path(workdir) / "source")
            output_path = str(Path(workdir) / "output.mp4")
            ass_path = str(Path(workdir) / "captions.ass") if spec.captions else None
            logo_path = str(Path(workdir) / "logo") if spec.logo else None
            source_generation = _download(spec.source_gcs_path, source_path)
            if ass_path:
                write_ass_captions(spec, ass_path)
            if logo_path and spec.logo:
                _download(str(spec.logo["gcs_path"]), logo_path)

            strategy_version = FOCAL_STRATEGY_VERSION
            used_v10 = False
            extras: dict[str, Any] = {}
            if spec.mode == "center_crop" and AUTOFRAME_ENGINE == "v10":
                from autoframe_v10 import AUTOFRAME_STRATEGY_VERSION

                strategy_version = AUTOFRAME_STRATEGY_VERSION
                used_v10 = True
                try:
                    extras = _render_with_v10(
                        spec, source_path, output_path, workdir,
                        source_generation=source_generation,
                        video_id=video_id, chapter_id=chapter_id,
                        ass_path=ass_path, logo_path=logo_path,
                    )
                except Exception:
                    # Deterministic center crop — never the legacy detector —
                    # so a v10 failure cannot silently degrade to old framing.
                    logger.exception("v10 autoframe falhou; recorte central determinístico")
                    spec = dataclasses.replace(spec, automatic_focal_points=())
                    render(spec, source_path, output_path, ass_path=ass_path, logo_path=logo_path)
            else:
                spec = _add_automatic_focus(spec, source_path)
                render(spec, source_path, output_path, ass_path=ass_path, logo_path=logo_path)
            output_uri = _upload(spec.output_gcs_path, output_path)

        update_status(video_id, render_id, chapter_id, "COMPLETED", output_gcs_path=output_uri, completed_at=firestore.SERVER_TIMESTAMP, error=firestore.DELETE_FIELD)
        # v10 returns no focal keyframes — the reusable artifact is the plan.
        points = [] if used_v10 else list(spec.automatic_focal_points)
        return ({"status": "COMPLETED", "video_id": video_id, "render_id": render_id, "chapter_id": chapter_id, "output_gcs_path": output_uri, "width": 1080, "height": 1920, "automatic_focal_points": points, "focal_strategy_version": strategy_version, **extras}, 200, _cors_headers())
    except ValidationError as exc:
        # Some style values are validated while assembling the filter graph.
        update_status(video_id, render_id, chapter_id, "ERROR", error=str(exc))
        return ({"error": str(exc)}, 400, _cors_headers())
    except Exception as exc:
        logger.exception("Vertical render failed")
        public_error = "render failed"
        update_status(video_id, render_id, chapter_id, "ERROR", error=str(exc)[:1500])
        return ({"error": public_error, "render_id": render_id}, 500, _cors_headers())
