"""Admin-editable prompt registry; overrides persist in Firestore.

Defaults live in this module so callers keep a deterministic fallback. An
edited prompt is stored per key under ``settings.COLLECTION_PROMPTS`` and
wins over the default until it is reverted (the document is deleted).
"""
from __future__ import annotations

from google.cloud import firestore

from app.config import settings
from app.db import get_db

CHAPTER_SUGGESTIONS = "chapter_suggestions"

DEFAULT_CHAPTER_PROMPT = """
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

PROMPT_DEFINITIONS = {
    CHAPTER_SUGGESTIONS: {
        "label": "Sugestão de capítulos",
        "description": "Prompt enviado ao Gemini para gerar os capítulos a partir das legendas. Variáveis disponíveis: {minimum} e {maximum} (duração mínima/máxima em segundos).",
        "default": DEFAULT_CHAPTER_PROMPT,
    },
}


def _document(key: str):
    return get_db().collection(settings.COLLECTION_PROMPTS).document(key)


def get_prompt(key: str) -> str:
    """Return the stored override for ``key`` or its default text."""

    snapshot = _document(key).get()
    if snapshot.exists:
        text = (snapshot.to_dict() or {}).get("text")
        if text:
            return text
    return PROMPT_DEFINITIONS[key]["default"]


def list_prompts() -> list[dict]:
    """Describe every prompt with its effective and original text."""

    result = []
    for key, definition in PROMPT_DEFINITIONS.items():
        snapshot = _document(key).get()
        stored = (snapshot.to_dict() or {}) if snapshot.exists else {}
        current = stored.get("text") or definition["default"]
        result.append(
            {
                "key": key,
                "label": definition["label"],
                "description": definition["description"],
                "current": current,
                "default": definition["default"],
                "edited": current != definition["default"],
                "updated_by": stored.get("updated_by"),
            }
        )
    return result


def save_prompt(key: str, text: str, updated_by: str | None) -> None:
    _document(key).set(
        {
            "text": text,
            "updated_by": updated_by,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )


def revert_prompt(key: str) -> None:
    _document(key).delete()
