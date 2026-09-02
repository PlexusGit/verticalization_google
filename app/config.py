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

    MAX_VIDEO_DURATION_SECONDS: int = 3600
    DAILY_VIDEO_LIMIT: int = 20
    DEFAULT_CHAPTER_MIN_SECONDS: int = 15
    DEFAULT_CHAPTER_MAX_SECONDS: int = 180
    OUTPUT_WIDTH: int = 1080
    OUTPUT_HEIGHT: int = 1920
    DEFAULT_CAPTION_FONT: str = "DejaVu Sans"
    DEFAULT_CAPTION_FONT_SIZE: int = 54
    # Must match FOCAL_STRATEGY_VERSION in vertical_render_function/face_detection.py;
    # cached focal points from older strategies are re-analyzed on the next render.
    FOCAL_STRATEGY_VERSION: int = 2

    SERVICE_NAME: str = "plexus-vertical-cuts"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=True,
    )


settings = Settings()

# An empty GOOGLE_APPLICATION_CREDENTIALS value blocks ADC fallback in Google
# client libraries. Treat an empty optional setting exactly like an absent one.
if not (settings.GOOGLE_APPLICATION_CREDENTIALS or "").strip():
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
