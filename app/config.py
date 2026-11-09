"""Typed configuration loaded from the single root ``.env`` file."""
from __future__ import annotations

import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by the web app and its service clients.

    Environment variable names intentionally stay explicit so operators can use
    the same file locally and in every deployment script without translation.
    """

    PROJECT_ID: str = Field(default="vertical-cuts-project", validation_alias="GOOGLE_CLOUD_PROJECT")
    LOCATION: str = "us"
    DEPLOY_REGION: str = "us-central1"
    GOOGLE_APPLICATION_CREDENTIALS: str | None = None
    FIRESTORE_DB_NAME: str = "(default)"
    BUCKET_NAME: str = "vertical-cuts-videos"

    GOOGLE_CLIENT_ID: str = ""
    SECRET_KEY: str = "development-only-change-me"
    DOMAIN: str = "http://localhost:8080"
    SECURITY_TOKEN: str = "development-only-change-me"

    COLLECTION_VIDEOS: str = "vertical_cuts_videos"
    COLLECTION_USERS: str = "vertical_cuts_users"
    ALLOWED_EMAILS_COLLECTION: str = "vertical_cuts_allowed_emails"
    COLLECTION_PROMPTS: str = "vertical_cuts_prompts"
    COLLECTION_TEMPLATES: str = "vertical_cuts_templates"

    TRANSCODE_URL: str = ""
    VERTICAL_RENDER_URL: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    CHIRP_LOCATION: str = "us"
    CHIRP_FALLBACK_LOCATION: str = "eu"

    # Speech-to-text engine: "gemini" (Gemini Transcribe, default) or "chirp"
    # (Speech-to-Text v2 Chirp 3). When Gemini fails after its own retries the
    # whole video is redone with Chirp 3 unless the fallback is disabled.
    SUBTITLE_ENGINE: str = "gemini"
    SUBTITLE_FALLBACK_TO_CHIRP: bool = True
    TRANSCRIBE_MODEL: str = "gemini-3.5-transcribe"
    # Comma-separated BCP-47 codes; empty = auto-detect.
    TRANSCRIBE_LANGUAGE_CODES: str = "pt-BR"
    TRANSCRIBE_CONCURRENCY: int = 3
    TRANSCRIBE_MAX_ATTEMPTS: int = 5
    TRANSCRIBE_REQUEST_TIMEOUT_SECONDS: int = 300

    # How long a transcode request keeps retrying while the worker has no free
    # instance (videos added at the same time queue behind each other).
    TRANSCODE_QUEUE_WAIT_SECONDS: int = 1800

    # Chapter renders run in parallel up to this many workers (the render
    # service scales to that many instances with concurrency=1).
    RENDER_PARALLELISM: int = 5
    # How long a render request keeps retrying while the render service has no
    # free instance (a burst of parallel renders queues behind cold starts).
    RENDER_QUEUE_WAIT_SECONDS: int = 1800

    # Cloud Run Job that runs long pipelines (survives idle shutdown and deploys
    # of the web service). Empty = run them inside the web process.
    PIPELINE_JOB_NAME: str = ""
    PIPELINE_JOB_REGION: str = "us-central1"

    MAX_VIDEO_DURATION_SECONDS: int = 3600
    DAILY_VIDEO_LIMIT: int = 20
    DEFAULT_CHAPTER_MIN_SECONDS: int = 15
    DEFAULT_CHAPTER_MAX_SECONDS: int = 180
    OUTPUT_WIDTH: int = 1080
    OUTPUT_HEIGHT: int = 1920
    DEFAULT_CAPTION_FONT: str = "DejaVu Sans"
    DEFAULT_CAPTION_FONT_SIZE: int = 54
    # Current autoframe strategy: 10 = v10 camera-plan artifacts (see
    # autoframe_v10/artifacts.py). LEGACY_FOCAL_STRATEGY_VERSION (3 = YuNet
    # focal keyframes) gates reuse of automatic_focal_points so renders on the
    # legacy engine still reuse their cached trajectories during migration.
    FOCAL_STRATEGY_VERSION: int = 10
    LEGACY_FOCAL_STRATEGY_VERSION: int = 3

    SERVICE_NAME: str = "plexus-vertical-cuts"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=True,
    )


DEFAULT_SECRET = "development-only-change-me"
MIN_SECRET_LENGTH = 32


def validate_production_secrets(config: Settings | None = None, environ=None) -> list[str]:
    """Refuse to run on Cloud Run with the development default secrets.

    ``SECRET_KEY`` signs session cookies and ``SECURITY_TOKEN`` is the shared
    API key, so the repository-visible default would let anyone in. Only the
    exact default is fatal; a short custom value is returned as a warning so a
    working deployment is never bricked by a restart. Cloud Run is detected by
    ``K_SERVICE`` (service) or ``CLOUD_RUN_JOB`` (pipeline job).
    """

    config = config or settings
    environ = os.environ if environ is None else environ
    secrets = {"SECRET_KEY": config.SECRET_KEY, "SECURITY_TOKEN": config.SECURITY_TOKEN}
    warnings = [
        f"{name} tem menos de {MIN_SECRET_LENGTH} caracteres; gere um valor mais forte (openssl rand -hex 32)"
        for name, value in secrets.items()
        if value != DEFAULT_SECRET and len(value) < MIN_SECRET_LENGTH
    ]
    if environ.get("K_SERVICE") or environ.get("CLOUD_RUN_JOB"):
        defaults = [name for name, value in secrets.items() if value == DEFAULT_SECRET]
        if defaults:
            raise RuntimeError(
                f"{' e '.join(defaults)} {'mantêm' if len(defaults) > 1 else 'mantém'} o valor padrão de desenvolvimento no Cloud Run; "
                "configure segredos próprios (veja ENV_GUIDE.md) antes de iniciar"
            )
    return warnings


settings = Settings()

# An empty GOOGLE_APPLICATION_CREDENTIALS value blocks ADC fallback in Google
# client libraries. Treat an empty optional setting exactly like an absent one.
if not (settings.GOOGLE_APPLICATION_CREDENTIALS or "").strip():
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
