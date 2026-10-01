"""Gemini-powered, subtitle-grounded hook and chapter recommendations."""
from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel, model_validator

from app.config import settings
from app.services import prompts


class SuggestedChapter(BaseModel):
    """Gemini-compatible chapter schema with post-response validation.

    Google GenAI's Schema protobuf rejects JSON Schema's ``exclusiveMinimum``.
    Numeric constraints therefore live in a model validator rather than Field,
    matching the simple structured-output approach proven in the RPP project.
    """

    start_seconds: float
    end_seconds: float
    title: str
    reason: str

    @model_validator(mode="after")
    def validate_time_range(self) -> "SuggestedChapter":
        if self.start_seconds < 0:
            raise ValueError("start_seconds must be non-negative")
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class ChapterSuggestions(BaseModel):
    chapters: list[SuggestedChapter]


def suggest_chapters(subtitle_text: str) -> list[SuggestedChapter]:
    """Return validated structured suggestions; configuration errors are explicit."""

    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não configurada")
    # Admin-edited overrides live in Firestore; an edit that drops the known
    # placeholders (or adds unknown ones) must not break the pipeline.
    try:
        prompt = prompts.get_prompt(prompts.CHAPTER_SUGGESTIONS).format(
            minimum=settings.DEFAULT_CHAPTER_MIN_SECONDS,
            maximum=settings.DEFAULT_CHAPTER_MAX_SECONDS,
        )
    except (KeyError, IndexError, ValueError):
        prompt = prompts.DEFAULT_CHAPTER_PROMPT.format(
            minimum=settings.DEFAULT_CHAPTER_MIN_SECONDS,
            maximum=settings.DEFAULT_CHAPTER_MAX_SECONDS,
        )
    response = genai.Client(api_key=settings.GEMINI_API_KEY).models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=f"{prompt}\n\n--- LEGENDAS SRT ---\n{subtitle_text}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ChapterSuggestions,
        ),
    )
    if not response.parsed:
        raise RuntimeError("Gemini não retornou capítulos estruturados")
    result = response.parsed
    if isinstance(result, ChapterSuggestions):
        return result.chapters
    return ChapterSuggestions.model_validate(result).chapters
