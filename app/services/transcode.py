"""HTTP client for the optional transcode Cloud Run worker."""
from __future__ import annotations

import requests
from app.config import settings


def submit(video_id: str, source_path: str) -> dict:
    if not settings.TRANSCODE_URL:
        raise RuntimeError("TRANSCODE_URL não configurada")
    response = requests.post(
        settings.TRANSCODE_URL.rstrip("/") + "/",
        json={
            "video_id": video_id,
            "gcs_source_path": source_path,
            "high_gcs_path": f"{video_id}/{video_id}_high.mp4",
        },
        headers={"X-API-Key": settings.SECURITY_TOKEN},
        timeout=3600,
    )
    response.raise_for_status()
    return response.json()
