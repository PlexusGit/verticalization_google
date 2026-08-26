"""Google One Tap login and session endpoints."""
from __future__ import annotations

import asyncio
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.cloud import firestore

from app.auth import get_allowed_user, verify_google_id_token
from app.config import settings
from app.db import get_db

router = APIRouter(tags=["auth"])


@router.post("/auth/onetap")
async def one_tap_login(request: Request, credential: str = Form(...)):
    """Verify the browser credential and open a signed cookie session."""

    user = await asyncio.to_thread(verify_google_id_token, credential)
    if not user:
        return JSONResponse({"error": "Não foi possível validar a conta Google"}, status_code=401)
    email = str(user.get("email", "")).strip().lower()
    db = get_db()
    allowed_user = await asyncio.to_thread(get_allowed_user, db, email)
    if not allowed_user:
        return JSONResponse({"error": f"O e-mail {email} não está autorizado"}, status_code=403)
    role = allowed_user["role"]
    user_id = str(user.get("sub", ""))
    db.collection(settings.COLLECTION_USERS).document(user_id).set(
        {
            "id": user_id,
            "email": email,
            "name": user.get("name", ""),
            "picture": user.get("picture", ""),
            "role": role,
            "last_login": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    request.session["user_id"] = user_id
    request.session["user_info"] = {"email": email, "name": user.get("name", ""), "picture": user.get("picture", ""), "role": role}
    return {"ok": True, "redirect": "/dashboard"}


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
