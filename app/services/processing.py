"""Durable orchestration for upload, transcription, chapters, and rendering."""
from __future__ import annotations

import hashlib
import os
import uuid

from google.cloud import firestore

from app.config import settings
from app.db import get_db
from app.models import RenderRequest, VideoStatus
from app.services import chirp, gemini, srt, storage, transcode, vertical_render


def create_video_record(
    title: str,
    description: str,
    filename: str,
    owner_email: str,
    *,
    source_type: str = "upload",
    source_id: str | None = None,
) -> tuple[str, bool]:
    """Create a video or return the existing deterministic importer record."""

    video_id = (
        f"v_import_{hashlib.sha256(source_id.encode()).hexdigest()[:16]}"
        if source_id
        else f"v_{uuid.uuid4().hex[:16]}"
    )
    ref = get_db().collection(settings.COLLECTION_VIDEOS).document(video_id)
    existing = ref.get()
    if existing.exists:
        return video_id, False
    ref.set(
        {
            "id": video_id,
            "title": title,
            "description": description,
            "source_filename": filename,
            "source_type": source_type,
            "source_id": source_id,
            "owner_email": owner_email,
            "status": VideoStatus.UPLOADING.value,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return video_id, True


def source_object_path(video_id: str, filename: str) -> str:
    extension = os.path.splitext(filename)[1].lower() or ".mp4"
    return f"{video_id}/{video_id}_source{extension}"


def store_uploaded_subtitles(video_id: str, subtitle_text: str) -> None:
    """Validate and persist optional user captions before processing."""

    if not srt.parse_srt(subtitle_text):
        raise ValueError("o arquivo SRT não contém legendas")
    object_path = f"{video_id}/{video_id}.srt"
    storage.upload_bytes(subtitle_text.encode(), object_path, "application/x-subrip")
    _video_ref(video_id).update(
        {
            "subtitle_text": subtitle_text,
            "subtitle_source": "user_upload",
            "gcs_srt_path": object_path,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )


def process_video(video_id: str) -> None:
    """Run resumable preprocessing and create automatic vertical drafts.

    Each expensive stage persists its output first. A retry can therefore reuse
    valid transcode or transcription artifacts instead of starting over.
    """

    ref = _video_ref(video_id)
    snapshot = ref.get()
    if not snapshot.exists:
        return
    data = snapshot.to_dict() or {}
    try:
        source_path = data.get("gcs_source_path")
        if not source_path or not storage.object_exists(source_path):
            raise RuntimeError("vídeo de origem não encontrado no armazenamento")

        high_path = data.get("gcs_high_path")
        audio_chunks = data.get("gcs_audio_chunks") or []
        if not high_path or not storage.object_exists(high_path) or not audio_chunks:
            _stage(ref, VideoStatus.TRANSCODING)
            result = transcode.submit(video_id, source_path)
            high_path = result["high_gcs_path"]
            audio_chunks = result.get("gcs_audio_chunks") or []
            duration = float(result.get("duration_seconds") or 0)
            if duration and duration > settings.MAX_VIDEO_DURATION_SECONDS:
                raise RuntimeError(
                    f"duração de {duration:.0f}s excede o limite de {settings.MAX_VIDEO_DURATION_SECONDS}s"
                )
            resolution = result.get("video_resolution") or result.get("resolution") or {}
            ref.update(
                {
                    "gcs_high_path": high_path,
                    "gcs_audio_chunks": audio_chunks,
                    "duration_seconds": duration,
                    "video_resolution": {
                        "w": int(resolution.get("w") or resolution.get("width") or 0),
                        "h": int(resolution.get("h") or resolution.get("height") or 0),
                    },
                    "updated_at": firestore.SERVER_TIMESTAMP,
                }
            )

        data = ref.get().to_dict() or {}
        subtitle_text = (data.get("subtitle_text") or "").strip()
        if not subtitle_text:
            _stage(ref, VideoStatus.TRANSCRIBING)
            subtitle_text = chirp.transcribe_chunks(audio_chunks)
            srt_path = f"{video_id}/{video_id}.srt"
            storage.upload_bytes(subtitle_text.encode(), srt_path, "application/x-subrip")
            ref.update(
                {
                    "subtitle_text": subtitle_text,
                    "subtitle_source": "chirp_3_pt_br",
                    "gcs_srt_path": srt_path,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                }
            )

        _stage(ref, VideoStatus.SUGGESTING)
        suggestions = gemini.suggest_chapters(subtitle_text)
        if not suggestions:
            raise RuntimeError("nenhum capítulo foi sugerido")

        chapter_refs = []
        for index, suggestion in enumerate(suggestions, start=1):
            chapter_id = f"chapter_{index:03d}"
            chapter_ref = ref.collection("chapters").document(chapter_id)
            chapter_subtitles = srt.slice_srt(
                subtitle_text,
                suggestion.start_seconds,
                suggestion.end_seconds,
            )
            chapter_ref.set(
                {
                    "id": chapter_id,
                    "title": suggestion.title,
                    "reason": suggestion.reason,
                    "start_seconds": suggestion.start_seconds,
                    "end_seconds": suggestion.end_seconds,
                    "subtitle_text": chapter_subtitles,
                    "selected": True,
                    "draft_status": "QUEUED",
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            chapter_refs.append((chapter_id, chapter_ref))

        _stage(ref, VideoStatus.RENDERING_DRAFTS)
        errors: list[str] = []
        for chapter_id, chapter_ref in chapter_refs:
            chapter = chapter_ref.get().to_dict() or {}
            try:
                render_chapter(video_id, chapter_id, RenderRequest(), final=False)
            except Exception as exc:
                errors.append(f"{chapter_id}: {exc}")
        if errors:
            raise RuntimeError("falha ao renderizar rascunhos: " + "; ".join(errors))
        _stage(ref, VideoStatus.PENDING_REVIEW, clear_error=True)
    except Exception as exc:
        _set_error(ref, exc)


def render_chapter(video_id: str, chapter_id: str, options: RenderRequest, *, final: bool) -> dict:
    """Persist and dispatch one draft/final render in a retry-safe path."""

    video_ref = _video_ref(video_id)
    video = video_ref.get().to_dict() or {}
    chapter_ref = video_ref.collection("chapters").document(chapter_id)
    snapshot = chapter_ref.get()
    if not snapshot.exists:
        raise KeyError(f"capítulo {chapter_id} não encontrado")
    chapter = snapshot.to_dict() or {}
    kind = "final" if final else "draft"
    render_id = f"{kind}_{uuid.uuid4().hex[:10]}"
    target = f"{video_id}/chapters/{chapter_id}/{render_id}.mp4"
    render_ref = chapter_ref.collection("renders").document(render_id)
    persisted_options = options.model_dump(mode="json")
    render_ref.set(
        {
            "id": render_id,
            "kind": kind,
            "status": "QUEUED",
            "target_gcs_path": target,
            "options": persisted_options,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    chapter_ref.update({f"{kind}_status": "PROCESSING", "updated_at": firestore.SERVER_TIMESTAMP})

    chapter_start = float(chapter["start_seconds"])
    captions = [
        {
            "start": cue.start + chapter_start,
            "end": cue.end + chapter_start,
            "text": cue.text,
        }
        for cue in srt.parse_srt(chapter.get("subtitle_text", ""))
    ]
    branding = persisted_options.get("branding") or {}
    logo = branding.get("logo")
    if logo:
        logo = {
            "gcs_path": logo["gcs_path"],
            "position": logo.get("position", "top-right").replace("-", "_"),
            "size": round(settings.OUTPUT_WIDTH * float(logo.get("width_percent", 18)) / 100),
            "opacity": logo.get("opacity", 1),
        }
    text = branding.get("text")
    text_overlays = []
    if text and text.get("text"):
        position = {"top": "top_center", "middle": "center", "bottom": "bottom_center"}.get(
            text.get("position"), "top_center"
        )
        text_overlays.append(
            {
                "text": text["text"],
                "position": position,
                "size": text.get("font_size", 48),
                "color": text.get("color", "#FFFFFF"),
                "opacity": 1,
            }
        )
    caption_style = persisted_options.get("caption_style") or {}
    payload = {
        "video_id": video_id,
        "chapter_id": chapter_id,
        "render_id": render_id,
        "source_gcs_path": video.get("gcs_high_path") or video.get("gcs_source_path"),
        "target_gcs_path": target,
        "trim_start": chapter_start,
        "trim_end": float(chapter["end_seconds"]),
        "video_resolution": video.get("video_resolution") or {},
        "mode": persisted_options.get("layout_mode", "center_crop"),
        "keyframes_top": persisted_options.get("keyframes_top") or [],
        "keyframes_bottom": persisted_options.get("keyframes_bottom") or [],
        "captions": captions,
        "caption_style": {
            "font": caption_style.get("font", settings.DEFAULT_CAPTION_FONT),
            "size": caption_style.get("font_size", settings.DEFAULT_CAPTION_FONT_SIZE),
            "color": caption_style.get("color", "#FFFFFF"),
            "outline_color": caption_style.get("outline_color", "#000000"),
            "outline_width": caption_style.get("outline_width", 3),
            "position": caption_style.get("position", "bottom"),
        },
        "logo": logo,
        "text_overlays": text_overlays,
    }
    try:
        result = vertical_render.submit(payload)
        output_path = result.get("gcs_path") or result.get("output_gcs_path") or target
        managed_prefix = f"gs://{settings.BUCKET_NAME}/"
        if output_path.startswith(managed_prefix):
            output_path = output_path[len(managed_prefix) :]
        render_ref.update(
            {
                "status": "COMPLETED",
                "gcs_path": output_path,
                "completed_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        chapter_ref.update(
            {
                f"{kind}_status": "COMPLETED",
                f"{kind}_gcs_path": output_path,
                f"{kind}_render_id": render_id,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return {"render_id": render_id, "gcs_path": output_path, "status": "COMPLETED"}
    except Exception as exc:
        render_ref.update({"status": "ERROR", "error": str(exc), "updated_at": firestore.SERVER_TIMESTAMP})
        chapter_ref.update({f"{kind}_status": "ERROR", f"{kind}_error": str(exc), "updated_at": firestore.SERVER_TIMESTAMP})
        raise


def render_final_chapters(video_id: str, chapter_ids: list[str]) -> None:
    """Render selected chapters and complete the parent only if all succeed."""

    video_ref = _video_ref(video_id)
    _stage(video_ref, VideoStatus.RENDERING_FINALS)
    errors: list[str] = []
    for chapter_id in chapter_ids:
        chapter = video_ref.collection("chapters").document(chapter_id).get().to_dict() or {}
        raw_options = chapter.get("render_options") or {}
        raw_options["final"] = True
        try:
            render_chapter(video_id, chapter_id, RenderRequest.model_validate(raw_options), final=True)
        except Exception as exc:
            errors.append(f"{chapter_id}: {exc}")
    if errors:
        _set_error(video_ref, RuntimeError("; ".join(errors)))
    else:
        _stage(video_ref, VideoStatus.COMPLETED, clear_error=True)
        video_ref.update({"completed_at": firestore.SERVER_TIMESTAMP})


def _video_ref(video_id: str):
    return get_db().collection(settings.COLLECTION_VIDEOS).document(video_id)


def _stage(ref, status: VideoStatus, *, clear_error: bool = False) -> None:
    fields: dict = {"status": status.value, "updated_at": firestore.SERVER_TIMESTAMP}
    if clear_error:
        fields["error"] = firestore.DELETE_FIELD
    ref.update(fields)


def _set_error(ref, error: Exception) -> None:
    ref.update(
        {
            "status": VideoStatus.ERROR.value,
            "error": str(error),
            "error_type": type(error).__name__,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    print(f"[pipeline] {type(error).__name__}: {error}", flush=True)
