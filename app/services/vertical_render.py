"""Dispatch chapter render jobs to the vertical renderer service."""
from __future__ import annotations

import datetime
import logging
import time

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from app.config import settings
from app.db import get_db

logger = logging.getLogger("plexus.render")

_TRANSIENT_NETWORK_ERRORS = (
    RequestsConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ReadTimeout,
)
_MAX_RETRIES = 2
_RETRY_DELAYS = [10, 30]

# The worker keeps rendering after the gateway drops the HTTP connection, so
# these responses say nothing about the render itself.
GATEWAY_STATUS_CODES = {502, 503, 504}
# A worker request cannot outlive the Cloud Run request timeout (3600 s).
WORKER_MAX_RENDER_SECONDS = 3600 + 120
_sleep = time.sleep


class RenderFailed(RuntimeError):
    """The worker recorded an error for this render."""


class RenderUnavailable(RuntimeError):
    """No usable worker result exists: it never started, or it was lost."""


def is_gateway_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return isinstance(exc, requests.HTTPError) and getattr(response, "status_code", None) in GATEWAY_STATUS_CODES


def is_recoverable(exc: Exception) -> bool:
    """Connection-level failures after which the worker may still finish the render."""

    return is_gateway_error(exc) or isinstance(exc, _TRANSIENT_NETWORK_ERRORS)


def _render_doc(video_id: str, render_id: str):
    return (
        get_db().collection(settings.COLLECTION_VIDEOS).document(video_id)
        .collection("renders").document(render_id)
    )


def worker_state(video_id: str, render_id: str) -> str:
    """The worker's own record of a render: COMPLETED, PROCESSING, ERROR or MISSING."""

    snapshot = _render_doc(video_id, render_id).get()
    if not snapshot.exists:
        return "MISSING"
    return str((snapshot.to_dict() or {}).get("status") or "MISSING")


def await_result(
    video_id: str,
    render_id: str,
    *,
    appear_timeout: float = 180,
    poll_seconds: float = 10,
) -> dict:
    """Wait for the worker's recorded result of ``render_id`` and return it.

    Raises ``RenderFailed`` when the worker recorded an error and
    ``RenderUnavailable`` when it never recorded the render (within
    ``appear_timeout``) or was apparently lost mid-render.
    """

    waited_since = time.monotonic()
    while True:
        if time.monotonic() - waited_since > WORKER_MAX_RENDER_SECONDS:
            raise RenderUnavailable("tempo máximo de espera pelo render excedido")
        snapshot = _render_doc(video_id, render_id).get()
        data = (snapshot.to_dict() or {}) if snapshot.exists else {}
        status = data.get("status")
        if status == "COMPLETED":
            return data.get("result") or {"status": "COMPLETED", "output_gcs_path": data.get("output_gcs_path")}
        if status == "ERROR":
            raise RenderFailed(str(data.get("error") or "o worker registrou uma falha"))
        if status == "PROCESSING":
            started = data.get("started_at")
            if isinstance(started, datetime.datetime):
                started = started if started.tzinfo else started.replace(tzinfo=datetime.timezone.utc)
                age = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
                if age > WORKER_MAX_RENDER_SECONDS:
                    raise RenderUnavailable(f"o worker não concluiu o render em {int(age)} s")
        elif time.monotonic() - waited_since > appear_timeout:
            raise RenderUnavailable("o worker não registrou este render")
        _sleep(poll_seconds)


def submit(payload: dict) -> dict:
    """Call the synchronous worker; persisted state makes dispatch retry-safe.

    Transient network errors (RemoteDisconnected, connection resets, read
    timeouts) are retried with backoff.  The render worker is idempotent —
    each call carries a unique render_id and target GCS path, so retrying
    after a dropped connection does not produce duplicate outputs.
    """

    if not settings.VERTICAL_RENDER_URL:
        raise RuntimeError("VERTICAL_RENDER_URL não configurada")
    url = settings.VERTICAL_RENDER_URL.rstrip("/") + "/"
    headers = {"X-API-Key": settings.SECURITY_TOKEN}
    logger.info("Enviando render ao worker %s (render_id=%s, capítulo=%s)", url, payload.get("render_id"), payload.get("chapter_id"))
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=3600)
            response.raise_for_status()
            return response.json()
        except _TRANSIENT_NETWORK_ERRORS as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "Render worker connection failed (attempt %d/%d): %s; retrying in %ds",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue
            raise
        except requests.HTTPError:
            raise
    raise last_exc  # type: ignore[misc]
