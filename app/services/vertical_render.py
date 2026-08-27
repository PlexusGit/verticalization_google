"""Dispatch chapter render jobs to the vertical renderer service."""
from __future__ import annotations

import logging
import time

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from app.config import settings

logger = logging.getLogger("plexus.render")

_TRANSIENT_NETWORK_ERRORS = (
    RequestsConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ReadTimeout,
)
_MAX_RETRIES = 2
_RETRY_DELAYS = [10, 30]


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
