"""Authenticated JSON API for uploads, review, imports, and rendering."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import tempfile
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import ValidationError

from app.auth import get_current_user, request_is_admin, request_is_authenticated
from app.config import settings
from app.db import get_db
from app.models import AllowedUserInput, ApprovalRequest, ChapterInput, ImportGCSRequest, RenderRequest, VideoStatus, VideoTemplateInput
from app.services import gemini, srt, storage
from app.services.processing import (
    create_video_record,
    process_video,
    render_chapter,
    render_final_chapters,
    source_object_path,
    store_uploaded_subtitles,
)

router = APIRouter(prefix="/api", tags=["api"])
_ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mxf"}
_ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _unauthorized(request: Request) -> JSONResponse | None:
    if request_is_authenticated(request):
        return None
    return JSONResponse({"error": "Não autorizado"}, status_code=401)


def _admin_forbidden(request: Request) -> JSONResponse | None:
    if not get_current_user(request):
        return JSONResponse({"error": "Não autorizado"}, status_code=401)
    if not request_is_admin(request):
        return JSONResponse({"error": "Acesso restrito a administradores"}, status_code=403)
    return None


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _video_snapshot(video_id: str):
    return get_db().collection(settings.COLLECTION_VIDEOS).document(video_id).get()


def _approval_block_reason(video_data: dict) -> str | None:
    """Explain why a video cannot be approved in its current lifecycle state."""

    if video_data.get("approved_at") or video_data.get("status") in {
        VideoStatus.RENDERING_FINALS.value,
        VideoStatus.COMPLETED.value,
    }:
        return "Os capítulos deste vídeo já foram aprovados"
    if video_data.get("status") != VideoStatus.PENDING_REVIEW.value:
        return "Os capítulos ainda não estão prontos para aprovação"
    return None


def _template_branding(template: dict, video: dict, chapter: dict) -> dict:
    title = str(template.get("title_macro") or "{chapter_title}")
    title = title.replace("{chapter_title}", str(chapter.get("title") or ""))
    title = title.replace("{video_title}", str(video.get("title") or ""))
    geometry = template.get("title_geometry") or {}
    return {
        "logo": template.get("logo"),
        "text": {
            "text": title,
            "position": template.get("title_position") or "top-center",
            "font_size": geometry.get("font_size", 54),
            "color": "#FFFFFF",
            "background_color": "#000000",
            "background_opacity": 0.55,
            "x_percent": geometry.get("x_percent"),
            "y_percent": geometry.get("y_percent"),
            "width_percent": geometry.get("width_percent"),
            "height_percent": geometry.get("height_percent"),
        },
    }


def _daily_limit_reached(owner_email: str) -> bool:
    """Enforce the configured per-owner limit without a composite index."""

    today = datetime.now(timezone.utc).date()
    query = get_db().collection(settings.COLLECTION_VIDEOS).where(
        filter=FieldFilter("owner_email", "==", owner_email)
    )
    count = 0
    for document in query.stream():
        created = (document.to_dict() or {}).get("created_at")
        if created and created.astimezone(timezone.utc).date() == today:
            count += 1
            if count >= settings.DAILY_VIDEO_LIMIT:
                return True
    return False


@router.post("/upload/init")
async def upload_init(request: Request):
    """Create a record and direct-to-GCS resumable upload session."""

    if response := _unauthorized(request):
        return response
    body = await request.json()
    title = str(body.get("title", "")).strip()
    filename = os.path.basename(str(body.get("filename", "video.mp4")))
    extension = os.path.splitext(filename)[1].lower()
    if not title:
        return JSONResponse({"error": "O título é obrigatório"}, status_code=400)
    if extension not in _ALLOWED_VIDEO_EXTENSIONS:
        return JSONResponse({"error": f"Formato não suportado: {extension}"}, status_code=400)

    user = get_current_user(request)
    owner = user["info"].get("email", "") if user else "api"
    if _daily_limit_reached(owner):
        return JSONResponse({"error": "Limite diário de vídeos atingido"}, status_code=429)
    video_id, _ = create_video_record(title, str(body.get("description", "")), filename, owner)
    object_path = source_object_path(video_id, filename)
    ref = get_db().collection(settings.COLLECTION_VIDEOS).document(video_id)
    ref.update({"gcs_source_path": object_path, "updated_at": firestore.SERVER_TIMESTAMP})
    try:
        session_url = storage.create_resumable_upload_session(
            object_path,
            str(body.get("content_type") or "video/mp4"),
            request.headers.get("origin") or settings.DOMAIN,
            int(body.get("size_bytes") or 0),
        )
    except Exception as exc:
        ref.update({"status": VideoStatus.ERROR.value, "error": str(exc)})
        return JSONResponse({"error": f"Não foi possível iniciar o upload: {exc}"}, status_code=500)
    return {"video_id": video_id, "session_url": session_url, "content_type": body.get("content_type") or "video/mp4"}


@router.post("/upload/finalize")
async def upload_finalize(request: Request, background_tasks: BackgroundTasks):
    """Validate the uploaded object and start preprocessing."""

    if response := _unauthorized(request):
        return response
    body = await request.json()
    video_id = str(body.get("video_id", "")).strip()
    snapshot = _video_snapshot(video_id)
    if not snapshot.exists:
        return JSONResponse({"error": "Upload não encontrado"}, status_code=404)
    data = snapshot.to_dict() or {}
    if data.get("status") not in {VideoStatus.UPLOADING.value, VideoStatus.ERROR.value}:
        return JSONResponse({"error": "O processamento deste vídeo já foi iniciado"}, status_code=409)
    object_path = data.get("gcs_source_path")
    if not object_path or not storage.object_exists(object_path):
        return JSONResponse({"error": "Arquivo não encontrado no GCS"}, status_code=400)
    subtitle_text = str(body.get("subtitle_text") or "").strip()
    try:
        if subtitle_text:
            store_uploaded_subtitles(video_id, subtitle_text)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    snapshot.reference.update({"status": VideoStatus.TRANSCODING.value, "updated_at": firestore.SERVER_TIMESTAMP})
    background_tasks.add_task(process_video, video_id)
    return JSONResponse({"video_id": video_id, "status": VideoStatus.TRANSCODING.value}, status_code=202)


@router.post("/videos")
async def multipart_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(...),
    description: str = Form(""),
    subtitle_file: UploadFile | None = File(None),
):
    """Small/headless-client upload alternative to resumable browser upload."""

    if response := _unauthorized(request):
        return response
    filename = os.path.basename(file.filename or "video.mp4")
    if os.path.splitext(filename)[1].lower() not in _ALLOWED_VIDEO_EXTENSIONS:
        return JSONResponse({"error": "Formato de vídeo não suportado"}, status_code=400)
    user = get_current_user(request)
    owner = user["info"].get("email", "") if user else "api"
    if _daily_limit_reached(owner):
        return JSONResponse({"error": "Limite diário de vídeos atingido"}, status_code=429)
    video_id, _ = create_video_record(title.strip(), description, filename, owner)
    object_path = source_object_path(video_id, filename)

    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1]) as temporary:
        while chunk := await file.read(8 * 1024 * 1024):
            temporary.write(chunk)
        temporary.flush()
        if temporary.tell() == 0:
            return JSONResponse({"error": "Arquivo vazio"}, status_code=400)
        storage.upload_file(temporary.name, object_path, file.content_type or "video/mp4")
    _video_snapshot(video_id).reference.update(
        {"gcs_source_path": object_path, "status": VideoStatus.TRANSCODING.value, "updated_at": firestore.SERVER_TIMESTAMP}
    )
    if subtitle_file and subtitle_file.filename:
        raw = await subtitle_file.read()
        try:
            store_uploaded_subtitles(video_id, raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            return JSONResponse({"error": f"SRT inválido: {exc}"}, status_code=400)
    background_tasks.add_task(process_video, video_id)
    return JSONResponse({"video_id": video_id, "status": VideoStatus.TRANSCODING.value}, status_code=202)


@router.post("/import/gcs")
async def import_gcs(request: Request, background_tasks: BackgroundTasks):
    """Idempotently import a GCS object submitted by either bucket function."""

    if response := _unauthorized(request):
        return response
    try:
        payload = ImportGCSRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()}, status_code=422)
    extension = os.path.splitext(payload.path or "")[1].lower()
    if extension not in _ALLOWED_VIDEO_EXTENSIONS:
        return JSONResponse({"error": "Objeto não é um vídeo suportado"}, status_code=400)
    original_key = payload.original_path or payload.source_metadata.get("key") or payload.path or "video.mp4"
    filename = os.path.basename(original_key)
    video_id, created = create_video_record(
        filename,
        f"Importado de {payload.source_type.upper()}",
        filename,
        "bucket-import",
        source_type=payload.source_type,
        source_id=payload.source_id,
    )
    if not created:
        data = _video_snapshot(video_id).to_dict() or {}
        return {"video_id": video_id, "status": data.get("status"), "created": False}
    managed_path = source_object_path(video_id, filename)
    try:
        if payload.bucket == settings.BUCKET_NAME:
            if payload.path != managed_path:
                storage.copy_object(payload.bucket, payload.path, managed_path)
        else:
            storage.copy_object(payload.bucket, payload.path, managed_path)
    except Exception as exc:
        _video_snapshot(video_id).reference.update({"status": VideoStatus.ERROR.value, "error": str(exc)})
        raise
    _video_snapshot(video_id).reference.update(
        {
            "gcs_source_path": managed_path,
            "import_metadata": payload.model_dump(mode="json"),
            "status": VideoStatus.TRANSCODING.value,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    background_tasks.add_task(process_video, video_id)
    return JSONResponse({"video_id": video_id, "status": VideoStatus.TRANSCODING.value, "created": True}, status_code=202)


@router.get("/users")
async def list_users(request: Request):
    if response := _admin_forbidden(request):
        return response
    users = []
    for document in get_db().collection(settings.ALLOWED_EMAILS_COLLECTION).stream():
        data = document.to_dict() or {}
        users.append(
            {
                "email": data.get("email") or document.id,
                "role": data.get("role") or "admin",
                "created_at": data.get("created_at"),
                "created_by": data.get("created_by"),
            }
        )
    return JSONResponse(_serialize({"users": sorted(users, key=lambda user: user["email"])}))


@router.post("/users")
async def create_user(request: Request):
    if response := _admin_forbidden(request):
        return response
    try:
        payload = AllowedUserInput.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()}, status_code=422)
    reference = get_db().collection(settings.ALLOWED_EMAILS_COLLECTION).document(payload.email)
    if reference.get().exists:
        return JSONResponse({"error": "Este usuário já está autorizado"}, status_code=409)
    current_user = get_current_user(request) or {}
    reference.set(
        {
            "email": payload.email,
            "role": payload.role,
            "created_by": (current_user.get("info") or {}).get("email", ""),
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return JSONResponse({"email": payload.email, "role": payload.role}, status_code=201)


@router.get("/templates")
async def list_templates(request: Request):
    if response := _unauthorized(request):
        return response
    templates = []
    for document in get_db().collection(settings.COLLECTION_TEMPLATES).stream():
        data = document.to_dict() or {}
        logo_path = (data.get("logo") or {}).get("gcs_path")
        if logo_path:
            try:
                data["logo_preview_url"] = storage.signed_url(logo_path, 60)
            except Exception:
                data["logo_preview_url"] = None
        templates.append(data)
    templates.sort(key=lambda item: (item.get("title") or "").lower())
    return JSONResponse(_serialize({"templates": templates}))


@router.post("/templates")
async def create_template(
    request: Request,
    title: str = Form(...),
    title_macro: str = Form("{chapter_title}"),
    logo_position: str = Form("top-right"),
    title_position: str = Form("top-center"),
    logo_x_percent: float = Form(68),
    logo_y_percent: float = Form(5),
    logo_width_percent: float = Form(27),
    logo_height_percent: float = Form(10),
    title_x_percent: float = Form(10),
    title_y_percent: float = Form(18),
    title_width_percent: float = Form(80),
    title_height_percent: float = Form(12),
    title_font_size: int = Form(54),
    logo: UploadFile | None = File(None),
):
    if response := _unauthorized(request):
        return response
    try:
        payload = VideoTemplateInput.model_validate(
            {
                "title": title,
                "title_macro": title_macro,
                "logo_position": logo_position,
                "title_position": title_position,
                "logo_x_percent": logo_x_percent,
                "logo_y_percent": logo_y_percent,
                "logo_width_percent": logo_width_percent,
                "logo_height_percent": logo_height_percent,
                "title_x_percent": title_x_percent,
                "title_y_percent": title_y_percent,
                "title_width_percent": title_width_percent,
                "title_height_percent": title_height_percent,
                "title_font_size": title_font_size,
            }
        )
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()}, status_code=422)
    collection = get_db().collection(settings.COLLECTION_TEMPLATES)
    reference = collection.document()
    logo_data = None
    if logo and logo.filename:
        extension = os.path.splitext(logo.filename)[1].lower()
        if extension not in _ALLOWED_LOGO_EXTENSIONS:
            return JSONResponse({"error": "Use PNG, JPG ou WebP"}, status_code=400)
        raw = await logo.read()
        if not raw or len(raw) > 10 * 1024 * 1024:
            return JSONResponse({"error": "Logo vazio ou maior que 10 MB"}, status_code=400)
        logo_path = f"templates/{reference.id}/logo{extension}"
        storage.upload_bytes(raw, logo_path, logo.content_type or "application/octet-stream")
        logo_data = {
            "gcs_path": logo_path,
            "position": payload.logo_position,
            "x_percent": payload.logo_x_percent,
            "y_percent": payload.logo_y_percent,
            "width_percent": payload.logo_width_percent,
            "height_percent": payload.logo_height_percent,
            "opacity": 1,
            "margin": 40,
        }
    current_user = get_current_user(request) or {}
    data = {
        "id": reference.id,
        "title": payload.title,
        "title_macro": payload.title_macro,
        "logo": logo_data,
        "title_position": payload.title_position,
        "title_geometry": {
            "x_percent": payload.title_x_percent,
            "y_percent": payload.title_y_percent,
            "width_percent": payload.title_width_percent,
            "height_percent": payload.title_height_percent,
            "font_size": payload.title_font_size,
        },
        "created_by": (current_user.get("info") or {}).get("email", ""),
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    reference.set(data)
    return JSONResponse({"id": reference.id, "title": payload.title}, status_code=201)


@router.get("/videos")
async def list_videos(request: Request, status: str | None = None):
    if response := _unauthorized(request):
        return response
    query = get_db().collection(settings.COLLECTION_VIDEOS).order_by("created_at", direction=firestore.Query.DESCENDING).limit(100)
    videos = []
    for document in query.stream():
        data = document.to_dict() or {}
        if status and data.get("status") != status:
            continue
        videos.append({key: data.get(key) for key in ("id", "title", "source_filename", "source_type", "status", "duration_seconds", "error", "created_at", "updated_at")})
    return JSONResponse(_serialize({"videos": videos}))


@router.get("/video/{video_id}")
async def video_detail(request: Request, video_id: str):
    if response := _unauthorized(request):
        return response
    snapshot = _video_snapshot(video_id)
    if not snapshot.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    data = snapshot.to_dict() or {}
    chapters = []
    for document in snapshot.reference.collection("chapters").stream():
        chapter = document.to_dict() or {}
        for kind in ("draft", "final"):
            path = chapter.get(f"{kind}_gcs_path")
            if path:
                try:
                    chapter[f"{kind}_preview_url"] = storage.signed_url(path, 60)
                except Exception:
                    chapter[f"{kind}_preview_url"] = None
        logo_path = (((chapter.get("render_options") or {}).get("branding") or {}).get("logo") or {}).get("gcs_path")
        if logo_path:
            try:
                chapter["logo_preview_url"] = storage.signed_url(logo_path, 60)
            except Exception:
                chapter["logo_preview_url"] = None
        chapters.append(chapter)
    data["chapters"] = sorted(chapters, key=lambda chapter: chapter.get("start_seconds", 0))
    return JSONResponse(_serialize(data))


@router.get("/video/{video_id}/source-url")
async def source_url(request: Request, video_id: str):
    if response := _unauthorized(request):
        return response
    snapshot = _video_snapshot(video_id)
    if not snapshot.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    data = snapshot.to_dict() or {}
    path = data.get("gcs_high_path") or data.get("gcs_source_path")
    if not path:
        return JSONResponse({"error": "Fonte indisponível"}, status_code=404)
    return {"url": storage.signed_url(path, 240)}


@router.get("/video/{video_id}/subtitles.srt")
async def subtitles(request: Request, video_id: str):
    if response := _unauthorized(request):
        return response
    snapshot = _video_snapshot(video_id)
    text = (snapshot.to_dict() or {}).get("subtitle_text", "") if snapshot.exists else ""
    if not text:
        return JSONResponse({"error": "Legendas indisponíveis"}, status_code=404)
    return PlainTextResponse(text, media_type="application/x-subrip", headers={"Content-Disposition": f'attachment; filename="{video_id}.srt"'})


@router.put("/video/{video_id}/chapters/{chapter_id}")
async def update_chapter(request: Request, video_id: str, chapter_id: str):
    """Save reviewer edits and derive relative subtitles when bounds change."""

    if response := _unauthorized(request):
        return response
    video = _video_snapshot(video_id)
    if not video.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    try:
        chapter = ChapterInput.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()}, status_code=422)
    if chapter.end_seconds > float((video.to_dict() or {}).get("duration_seconds") or chapter.end_seconds):
        return JSONResponse({"error": "O capítulo ultrapassa a duração do vídeo"}, status_code=400)
    data = chapter.model_dump(mode="json", exclude={"id"})
    if not data["subtitle_text"]:
        data["subtitle_text"] = srt.slice_srt((video.to_dict() or {}).get("subtitle_text", ""), chapter.start_seconds, chapter.end_seconds)
    data["updated_at"] = firestore.SERVER_TIMESTAMP
    video.reference.collection("chapters").document(chapter_id).set(data, merge=True)
    return {"ok": True}


@router.post("/video/{video_id}/chapters")
async def add_chapter(request: Request, video_id: str):
    if response := _unauthorized(request):
        return response
    video = _video_snapshot(video_id)
    if not video.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    try:
        chapter = ChapterInput.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()}, status_code=422)
    chapter_id = f"chapter_{video.reference.collection('chapters').document().id[:12]}"
    data = chapter.model_dump(mode="json", exclude={"id"})
    if not data["subtitle_text"]:
        data["subtitle_text"] = srt.slice_srt((video.to_dict() or {}).get("subtitle_text", ""), chapter.start_seconds, chapter.end_seconds)
    data.update({"id": chapter_id, "created_at": firestore.SERVER_TIMESTAMP, "updated_at": firestore.SERVER_TIMESTAMP})
    video.reference.collection("chapters").document(chapter_id).set(data)
    return JSONResponse({"chapter_id": chapter_id}, status_code=201)


@router.delete("/video/{video_id}/chapters/{chapter_id}")
async def delete_chapter(request: Request, video_id: str, chapter_id: str):
    if response := _unauthorized(request):
        return response
    snapshot = _video_snapshot(video_id)
    if not snapshot.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    snapshot.reference.collection("chapters").document(chapter_id).delete()
    return {"ok": True}


@router.post("/video/{video_id}/regenerate-chapters")
async def regenerate_chapters(request: Request, video_id: str):
    """Replace suggestions from the current canonical subtitles."""

    if response := _unauthorized(request):
        return response
    video = _video_snapshot(video_id)
    if not video.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    data = video.to_dict() or {}
    try:
        suggestions = gemini.suggest_chapters(data.get("subtitle_text", ""))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    for document in video.reference.collection("chapters").stream():
        document.reference.delete()
    for index, suggestion in enumerate(suggestions, start=1):
        chapter_id = f"chapter_{index:03d}"
        video.reference.collection("chapters").document(chapter_id).set(
            {
                "id": chapter_id,
                **suggestion.model_dump(mode="json"),
                "subtitle_text": srt.slice_srt(data.get("subtitle_text", ""), suggestion.start_seconds, suggestion.end_seconds),
                "selected": True,
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
    return {"count": len(suggestions)}


@router.post("/video/{video_id}/chapters/{chapter_id}/logo")
async def upload_logo(request: Request, video_id: str, chapter_id: str, file: UploadFile = File(...)):
    if response := _unauthorized(request):
        return response
    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in _ALLOWED_LOGO_EXTENSIONS:
        return JSONResponse({"error": "Use PNG, JPG ou WebP"}, status_code=400)
    raw = await file.read()
    if not raw or len(raw) > 10 * 1024 * 1024:
        return JSONResponse({"error": "Logo vazio ou maior que 10 MB"}, status_code=400)
    path = f"{video_id}/branding/{chapter_id}{extension}"
    storage.upload_bytes(raw, path, file.content_type or "application/octet-stream")
    return {"gcs_path": path}


@router.post("/video/{video_id}/chapters/{chapter_id}/render")
async def queue_render(request: Request, video_id: str, chapter_id: str, background_tasks: BackgroundTasks):
    if response := _unauthorized(request):
        return response
    try:
        options = RenderRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()}, status_code=422)
    chapter_ref = _video_snapshot(video_id).reference.collection("chapters").document(chapter_id)
    if not chapter_ref.get().exists:
        return JSONResponse({"error": "Capítulo não encontrado"}, status_code=404)
    chapter_ref.update({"render_options": options.model_dump(mode="json"), "updated_at": firestore.SERVER_TIMESTAMP})
    background_tasks.add_task(render_chapter, video_id, chapter_id, options, final=options.final)
    return JSONResponse({"status": "QUEUED"}, status_code=202)


@router.post("/video/{video_id}/approve")
async def approve(request: Request, video_id: str, background_tasks: BackgroundTasks):
    if response := _unauthorized(request):
        return response
    video = _video_snapshot(video_id)
    if not video.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    video_data = video.to_dict() or {}
    if reason := _approval_block_reason(video_data):
        return JSONResponse({"error": reason}, status_code=409)

    try:
        payload = ApprovalRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()}, status_code=422)
    selected = list(dict.fromkeys(payload.chapter_ids))
    available = {document.id: document for document in video.reference.collection("chapters").stream()}
    if any(chapter_id not in available for chapter_id in selected):
        return JSONResponse({"error": "Selecione capítulos válidos"}, status_code=400)

    templates = {}
    for chapter_id in selected:
        template_id = payload.template_ids.get(chapter_id)
        if not template_id or template_id in templates:
            continue
        snapshot = get_db().collection(settings.COLLECTION_TEMPLATES).document(template_id).get()
        if not snapshot.exists:
            return JSONResponse({"error": "Um dos templates selecionados não existe"}, status_code=400)
        templates[template_id] = snapshot.to_dict() or {}

    approved_templates = {}
    for chapter_id in selected:
        chapter_document = available[chapter_id]
        chapter = chapter_document.to_dict() or {}
        template_id = payload.template_ids.get(chapter_id)
        approved_templates[chapter_id] = template_id
        if not template_id:
            chapter_document.reference.set(
                {"template_id": None, "template_title": None, "template_snapshot": None, "updated_at": firestore.SERVER_TIMESTAMP},
                merge=True,
            )
            continue
        template = templates[template_id]
        raw_options = chapter.get("render_options") or {}
        raw_options["branding"] = _template_branding(template, video_data, chapter)
        options = RenderRequest.model_validate(raw_options).model_dump(mode="json")
        chapter_document.reference.set(
            {
                "template_id": template_id,
                "template_title": template.get("title"),
                "template_snapshot": template,
                "render_options": options,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

    # Persist the lock before dispatch so repeated clicks or concurrent requests
    # cannot submit the same set of final renders twice.
    video.reference.update(
        {
            "status": VideoStatus.RENDERING_FINALS.value,
            "approved_chapter_ids": selected,
            "approved_templates": approved_templates,
            "approved_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    background_tasks.add_task(render_final_chapters, video_id, selected)
    return JSONResponse({"status": VideoStatus.RENDERING_FINALS.value}, status_code=202)


@router.get("/video/{video_id}/chapters/{chapter_id}/{kind}/download")
async def download_render(request: Request, video_id: str, chapter_id: str, kind: str):
    if response := _unauthorized(request):
        return response
    if kind not in {"draft", "final"}:
        return JSONResponse({"error": "Tipo inválido"}, status_code=400)
    chapter = _video_snapshot(video_id).reference.collection("chapters").document(chapter_id).get()
    path = (chapter.to_dict() or {}).get(f"{kind}_gcs_path") if chapter.exists else None
    if not path:
        return JSONResponse({"error": "Render indisponível"}, status_code=404)
    return RedirectResponse(storage.signed_url(path, 60))


@router.post("/video/{video_id}/retry")
async def retry_video(request: Request, video_id: str, background_tasks: BackgroundTasks):
    if response := _unauthorized(request):
        return response
    snapshot = _video_snapshot(video_id)
    if not snapshot.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    current_status = (snapshot.to_dict() or {}).get("status")
    if current_status not in {VideoStatus.ERROR.value, VideoStatus.UPLOADING.value}:
        return JSONResponse({"error": "O vídeo não está em um estado que permita retry"}, status_code=409)
    snapshot.reference.update({"status": VideoStatus.TRANSCODING.value, "updated_at": firestore.SERVER_TIMESTAMP})
    background_tasks.add_task(process_video, video_id)
    return JSONResponse({"status": "RETRYING"}, status_code=202)


@router.delete("/video/{video_id}")
async def delete_video(request: Request, video_id: str):
    if response := _unauthorized(request):
        return response
    snapshot = _video_snapshot(video_id)
    if not snapshot.exists:
        return JSONResponse({"error": "Vídeo não encontrado"}, status_code=404)
    for chapter in snapshot.reference.collection("chapters").stream():
        for render in chapter.reference.collection("renders").stream():
            render.reference.delete()
        chapter.reference.delete()
    storage.delete_prefix(f"{video_id}/")
    snapshot.reference.delete()
    return {"ok": True}
