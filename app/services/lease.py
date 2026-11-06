"""Heartbeat lease that makes interrupted pipelines detectable and resumable.

Pipelines run as in-process background tasks. When their instance is replaced
(a deploy), scaled in, or killed, the task dies without writing any state and
the video would sit in a working status forever. While a pipeline runs it
therefore refreshes ``heartbeat_at`` on its video document; a video whose
heartbeat stopped is *stale* and can be atomically claimed and resumed by any
instance that notices it.
"""
from __future__ import annotations

import datetime
import logging
import os
import threading
import uuid
from contextlib import contextmanager

from google.cloud import firestore

from app.config import settings
from app.db import get_db
from app.models import VideoStatus

logger = logging.getLogger("plexus.lease")

HEARTBEAT_SECONDS = 30
LEASE_SECONDS = 150
MAX_RESUMES = 3
INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
WORKING_STATUSES = {
    VideoStatus.TRANSCODING.value,
    VideoStatus.TRANSCRIBING.value,
    VideoStatus.SUGGESTING.value,
    VideoStatus.RENDERING_DRAFTS.value,
    VideoStatus.RENDERING_FINALS.value,
}

_active: set[str] = set()
_active_lock = threading.Lock()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(value) -> datetime.datetime | None:
    if not isinstance(value, datetime.datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def is_active_here(video_id: str) -> bool:
    with _active_lock:
        return video_id in _active


def is_stale(data: dict, video_id: str | None = None, now: datetime.datetime | None = None) -> bool:
    """True when a video is in a working status but nothing has made progress lately.

    The newest of ``heartbeat_at`` and ``updated_at`` counts, so videos created
    before heartbeats existed are judged by their last write.
    """

    if data.get("status") not in WORKING_STATUSES:
        return False
    if video_id and is_active_here(video_id):
        return False
    seen = [t for t in (_as_utc(data.get("heartbeat_at")), _as_utc(data.get("updated_at"))) if t]
    if not seen:
        return True
    return ((now or _now()) - max(seen)).total_seconds() > LEASE_SECONDS


@contextmanager
def pipeline_lease(video_id: str):
    """Hold the lease for ``video_id`` and refresh its heartbeat until the block ends."""

    ref = get_db().collection(settings.COLLECTION_VIDEOS).document(video_id)
    stop = threading.Event()

    def beat() -> None:
        try:
            ref.update({"heartbeat_at": firestore.SERVER_TIMESTAMP, "lease_owner": INSTANCE_ID})
        except Exception:
            logger.warning("Falha ao renovar o heartbeat de %s", video_id, exc_info=True)

    def loop() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            beat()

    with _active_lock:
        _active.add(video_id)
    beat()
    thread = threading.Thread(target=loop, name=f"lease-{video_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        with _active_lock:
            _active.discard(video_id)
        try:
            ref.update({"lease_owner": firestore.DELETE_FIELD})
        except Exception:
            logger.warning("Falha ao liberar o lease de %s", video_id, exc_info=True)


def decide_claim(data: dict, video_id: str, now: datetime.datetime | None = None) -> tuple[str, dict]:
    """Decide what to do with a video seen in a working status.

    Returns ``("skip", {})`` when the pipeline is alive, ``("resume", fields)``
    to claim it, or ``("give_up", fields)`` once it was resumed too often.
    """

    if not is_stale(data, video_id, now):
        return "skip", {}
    resumes = int(data.get("resume_count") or 0)
    if resumes >= MAX_RESUMES:
        return "give_up", {
            "status": VideoStatus.ERROR.value,
            "error": f"O processamento foi interrompido {resumes} vezes seguidas. Use \"Tentar novamente\".",
            "error_type": "PipelineInterrupted",
            "lease_owner": firestore.DELETE_FIELD,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    return "resume", {
        "resume_count": resumes + 1,
        "lease_owner": INSTANCE_ID,
        "heartbeat_at": firestore.SERVER_TIMESTAMP,
    }


def claim_resume(video_id: str) -> str:
    """Atomically claim a stale video; returns ``"resume"``, ``"give_up"`` or ``"skip"``.

    The decision and the write happen in one Firestore transaction, so when
    several requests notice the same stale video at once only one resumes it.
    """

    db = get_db()
    ref = db.collection(settings.COLLECTION_VIDEOS).document(video_id)

    @firestore.transactional
    def attempt(transaction) -> str:
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return "skip"
        action, fields = decide_claim(snapshot.to_dict() or {}, video_id)
        if fields:
            transaction.update(ref, fields)
        return action

    return attempt(db.transaction())
