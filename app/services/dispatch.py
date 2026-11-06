"""Start long pipelines on a Cloud Run Job, or in-process when no job is configured.

A pipeline run as a background task of the web service is killed whenever Cloud
Run shuts that instance down (idle for ~15 minutes, a deploy, scale-in). Run as
a job execution it is not. Locally, or if the job cannot be started, the same
code runs in-process so a video is never left without a pipeline; the heartbeat
lease then covers an interrupted in-process run.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.config import settings

logger = logging.getLogger("plexus.dispatch")
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _project = google.auth.default(scopes=_SCOPES)
    return AuthorizedSession(credentials)


def run_job(env: dict[str, str]) -> None:
    """Start one execution of the pipeline job with per-run environment overrides."""

    name = f"projects/{settings.PROJECT_ID}/locations/{settings.PIPELINE_JOB_REGION}/jobs/{settings.PIPELINE_JOB_NAME}"
    response = _session().post(
        f"https://run.googleapis.com/v2/{name}:run",
        json={"overrides": {"containerOverrides": [{"env": [{"name": key, "value": value} for key, value in env.items()]}]}},
        timeout=30,
    )
    response.raise_for_status()


def start(
    background_tasks,
    kind: str,
    video_id: str,
    fallback: Callable,
    *,
    chapter_ids: list[str] | None = None,
    resume: bool = False,
) -> str:
    """Start a ``process`` or ``finals`` pipeline; returns ``"job"`` or ``"in_process"``."""

    if settings.PIPELINE_JOB_NAME:
        env = {
            "PIPELINE_KIND": kind,
            "PIPELINE_VIDEO_ID": video_id,
            "PIPELINE_RESUME": "1" if resume else "0",
            "PIPELINE_CHAPTER_IDS": ",".join(chapter_ids or []),
        }
        try:
            run_job(env)
            logger.info("Pipeline %s de %s iniciado no job %s", kind, video_id, settings.PIPELINE_JOB_NAME)
            return "job"
        except Exception:
            logger.exception("Não foi possível iniciar o job %s; executando no próprio serviço", settings.PIPELINE_JOB_NAME)
    args: list = [video_id]
    if kind == "finals":
        args.append(list(chapter_ids or []))
    if resume:
        args.append(True)
    background_tasks.add_task(fallback, *args)
    return "in_process"
