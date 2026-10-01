"""Validated API and renderer payload models."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class AllowedUserInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["admin", "user"] = "user"

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        local, separator, domain = normalized.partition("@")
        if not separator or not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("Informe um e-mail válido")
        return normalized


class VideoTemplateInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    title_macro: str = Field(default="{chapter_title}", min_length=1, max_length=240)
    logo_position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    title_position: Literal["top-left", "top-center", "top-right", "center", "bottom-left", "bottom-center", "bottom-right"] = "top-center"
    logo_x_percent: float = Field(default=68, ge=0, le=100)
    logo_y_percent: float = Field(default=5, ge=0, le=100)
    logo_width_percent: float = Field(default=27, ge=3, le=100)
    logo_height_percent: float = Field(default=10, ge=1, le=100)
    title_x_percent: float = Field(default=10, ge=0, le=100)
    title_y_percent: float = Field(default=18, ge=0, le=100)
    title_width_percent: float = Field(default=80, ge=5, le=100)
    title_height_percent: float = Field(default=12, ge=2, le=100)
    title_font_size: int = Field(default=54, ge=16, le=140)

    @model_validator(mode="after")
    def validate_geometry(self) -> "VideoTemplateInput":
        if self.logo_x_percent + self.logo_width_percent > 100 or self.logo_y_percent + self.logo_height_percent > 100:
            raise ValueError("A área do logo deve ficar dentro da tela vertical")
        if self.title_x_percent + self.title_width_percent > 100 or self.title_y_percent + self.title_height_percent > 100:
            raise ValueError("A área do título deve ficar dentro da tela vertical")
        return self


class ApprovalRequest(BaseModel):
    chapter_ids: list[str] = Field(min_length=1, max_length=100)
    template_ids: dict[str, str | None] = Field(default_factory=dict)


class VideoStatus(StrEnum):
    UPLOADING = "UPLOADING"
    TRANSCODING = "TRANSCODING"
    TRANSCRIBING = "TRANSCRIBING"
    SUGGESTING = "SUGGESTING"
    RENDERING_DRAFTS = "RENDERING_DRAFTS"
    PENDING_REVIEW = "PENDING_REVIEW"
    RENDERING_FINALS = "RENDERING_FINALS"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class RenderStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class ChapterInput(BaseModel):
    """Editable chapter boundaries and copy submitted by the review UI."""

    id: str | None = None
    title: str = Field(min_length=1, max_length=180)
    reason: str = Field(default="", max_length=600)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    subtitle_text: str = Field(default="", max_length=100_000)
    template_id: str | None = Field(default=None, max_length=200)
    selected: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> "ChapterInput":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class CropKeyframe(BaseModel):
    time: float = Field(ge=0)
    x: float = Field(ge=0)
    y: float = Field(ge=0)


class CaptionStyle(BaseModel):
    font: str = Field(default="DejaVu Sans", max_length=100)
    font_size: int = Field(default=54, ge=18, le=120)
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = Field(default=3, ge=0, le=12)
    position: Literal["top", "middle", "bottom"] = "bottom"

    @field_validator("color", "outline_color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 7 or not value.startswith("#") or any(c not in "0123456789ABCDEF" for c in value[1:]):
            raise ValueError("colors must use #RRGGBB")
        return value


class LogoOverlay(BaseModel):
    gcs_path: str = Field(default="", max_length=1024)
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    width_percent: float = Field(default=18, ge=3, le=100)
    height_percent: float | None = Field(default=None, ge=1, le=100)
    x_percent: float | None = Field(default=None, ge=0, le=100)
    y_percent: float | None = Field(default=None, ge=0, le=100)
    opacity: float = Field(default=1, ge=0, le=1)
    margin: int = Field(default=40, ge=0, le=300)


class TextOverlay(BaseModel):
    text: str = Field(default="", max_length=240)
    position: Literal["top", "middle", "bottom", "top-left", "top-center", "top-right", "center", "bottom-left", "bottom-center", "bottom-right"] = "top"
    font_size: int = Field(default=48, ge=16, le=140)
    color: str = "#FFFFFF"
    background_color: str = "#000000"
    background_opacity: float = Field(default=0.55, ge=0, le=1)
    x_percent: float | None = Field(default=None, ge=0, le=100)
    y_percent: float | None = Field(default=None, ge=0, le=100)
    width_percent: float | None = Field(default=None, ge=5, le=100)
    height_percent: float | None = Field(default=None, ge=2, le=100)

    @field_validator("color", "background_color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 7 or not value.startswith("#") or any(c not in "0123456789ABCDEF" for c in value[1:]):
            raise ValueError("colors must use #RRGGBB")
        return value


class BrandingOptions(BaseModel):
    logo: LogoOverlay | None = None
    text: TextOverlay | None = None


class RenderRequest(BaseModel):
    """User-controlled render request persisted before worker dispatch."""

    layout_mode: Literal["center_crop", "split_stack"] = "center_crop"
    keyframes_top: list[CropKeyframe] = Field(default_factory=list, max_length=200)
    keyframes_bottom: list[CropKeyframe] = Field(default_factory=list, max_length=200)
    automatic_focal_points: list[CropKeyframe] = Field(default_factory=list, max_length=241)
    caption_style: CaptionStyle = Field(default_factory=CaptionStyle)
    branding: BrandingOptions = Field(default_factory=BrandingOptions)
    final: bool = False
    # Strategy of the stored framing artifacts: 3 = legacy focal keyframes,
    # 10 = v10 camera plan. Written back from every worker response so final
    # renders reuse the exact draft analysis instead of re-detecting.
    focal_strategy_version: int = 0
    camera_plan_gcs_path: str = Field(default="", max_length=1024)
    camera_plan_fingerprint: str = Field(default="", max_length=128)


class ImportGCSRequest(BaseModel):
    """Managed GCS object submitted by either Pub/Sub importer.

    The canonical fields match the bundled functions. ``path``/``source_type``
    aliases remain accepted for simple third-party API clients.
    """

    bucket: str = Field(min_length=3, max_length=222)
    object_name: str | None = Field(default=None, max_length=1024)
    path: str | None = Field(default=None, max_length=1024)
    source: Literal["gcs", "s3"] | None = None
    source_type: Literal["gcs", "s3"] | None = None
    idempotency_key: str | None = Field(default=None, max_length=1200)
    source_id: str | None = Field(default=None, max_length=1200)
    gcs_uri: str | None = Field(default=None, max_length=1400)
    source_metadata: dict = Field(default_factory=dict)
    generation: str | None = Field(default=None, max_length=100)
    original_bucket: str | None = Field(default=None, max_length=222)
    original_path: str | None = Field(default=None, max_length=1024)
    etag: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def normalize_import(self) -> "ImportGCSRequest":
        self.path = self.path or self.object_name
        self.source_type = self.source_type or self.source or "gcs"
        self.source_id = self.source_id or self.idempotency_key
        if not self.path:
            raise ValueError("path or object_name is required")
        if not self.source_id:
            raise ValueError("source_id or idempotency_key is required")
        return self
