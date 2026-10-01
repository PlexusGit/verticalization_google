"""Server-rendered entry points for the lightweight review interface."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_current_user, request_is_admin
from app.config import settings

router = APIRouter(tags=["views"])


@router.get("/", response_class=HTMLResponse)
async def login(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {"google_client_id": settings.GOOGLE_CLIENT_ID},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "dashboard.html", {"user": user})


@router.get("/templates", response_class=HTMLResponse)
async def templates(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "templates.html", {"user": user})


@router.get("/users", response_class=HTMLResponse)
async def users(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    if not request_is_admin(request):
        return RedirectResponse("/dashboard", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "users.html", {"user": user})


@router.get("/prompts", response_class=HTMLResponse)
async def prompts(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    if not request_is_admin(request):
        return RedirectResponse("/dashboard", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "prompts.html", {"user": user})


@router.get("/video/{video_id}", response_class=HTMLResponse)
async def video_detail(request: Request, video_id: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "video_detail.html",
        {"user": user, "video_id": video_id},
    )
