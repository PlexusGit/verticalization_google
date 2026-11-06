"""HTTP client for the optional transcode Cloud Run worker."""
from __future__ import annotations

import logging
import time

import requests
from app.config import settings

logger = logging.getLogger("plexus.transcode")

INITIAL_WAIT_SECONDS = 15
MAX_WAIT_STEP_SECONDS = 60
_sleep = time.sleep


def is_capacity_rejection(response: requests.Response) -> bool:
    """True when Cloud Run refused the request because no instance was free.

    These responses come from the platform, not the worker: the request never
    started, so sending it again is safe. A genuine worker failure is a JSON
    error and is never retried here.
    """

    if response.status_code in (429, 503):
        return True
    return response.status_code == 500 and "available instance" in (response.text or "")


def submit(video_id: str, source_path: str) -> dict:
    if not settings.TRANSCODE_URL:
        raise RuntimeError("TRANSCODE_URL não configurada")
    started = time.monotonic()
    wait = INITIAL_WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
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
        if not is_capacity_rejection(response) or time.monotonic() - started + wait > settings.TRANSCODE_QUEUE_WAIT_SECONDS:
            response.raise_for_status()
            return response.json()
        logger.warning(
            "Worker de transcode sem instância livre para %s (tentativa %d, HTTP %s); nova tentativa em %ds",
            video_id, attempt, response.status_code, wait,
        )
        _sleep(wait)
        wait = min(wait * 2, MAX_WAIT_STEP_SECONDS)
