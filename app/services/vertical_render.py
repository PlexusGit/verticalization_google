"""Dispatch chapter render jobs to the vertical renderer service."""
from __future__ import annotations

import requests
from app.config import settings


def submit(payload: dict) -> dict:
    """Call the synchronous worker; persisted state makes dispatch retry-safe."""

    if not settings.VERTICAL_RENDER_URL:
        raise RuntimeError("VERTICAL_RENDER_URL não configurada")
    response = requests.post(
        settings.VERTICAL_RENDER_URL.rstrip("/") + "/",
        json=payload,
        headers={"X-API-Key": settings.SECURITY_TOKEN},
        timeout=3600,
    )
    response.raise_for_status()
    return response.json()
