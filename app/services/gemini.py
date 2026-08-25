"""Gemini-powered, subtitle-grounded hook and chapter recommendations."""
from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.config import settings


class SuggestedChapter(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    title: str
    reason: str


class ChapterSuggestions(BaseModel):
    chapters: list[SuggestedChapter]


_PROMPT = """
Você é um editor de vídeos jornalísticos em português brasileiro. Analise somente
as legendas SRT fornecidas e identifique trechos independentes com forte gancho
para redes sociais.

Regras obrigatórias:
- Use apenas fatos e nomes explicitamente presentes nas legendas.
- Retorne segundos numéricos alinhados aos limites das legendas.
- Cada capítulo deve ter entre {minimum} e {maximum} segundos.
- Ignore vinhetas, saudações, publicidade, repetição e transições.
- Títulos e justificativas devem estar em português brasileiro.
- Ordene cronologicamente e não sobreponha capítulos.
"""


def suggest_chapters(subtitle_text: str) -> list[SuggestedChapter]:
    """Return validated structured suggestions; configuration errors are explicit."""

    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não configurada")
    prompt = _PROMPT.format(
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
