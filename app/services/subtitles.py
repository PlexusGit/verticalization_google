"""Speech-to-text facade: picks the engine and handles the fallback.

The default engine is Gemini Transcribe (``app.services.transcribe``). If it
fails after its own retries, the whole video is redone with Chirp 3
(``app.services.chirp``) unless ``SUBTITLE_FALLBACK_TO_CHIRP`` is disabled.
``SUBTITLE_ENGINE=chirp`` skips Gemini entirely.

Both engines read the same MP3 chunks produced by the transcode worker, so
nothing is re-transcoded when the engine changes.
"""
from __future__ import annotations

from typing import Callable

from app.config import settings

ProgressCallback = Callable[[str, str, dict], None]
GEMINI_SOURCE = "gemini_transcribe"
CHIRP_SOURCE = "chirp_3_pt_br"


def _gemini(chunks: list[dict], callback: ProgressCallback | None) -> str:
    from app.services import transcribe

    return transcribe.transcribe_chunks(chunks, callback)


def _chirp(chunks: list[dict], callback: ProgressCallback | None) -> str:
    from app.services import chirp

    return chirp.transcribe_chunks(chunks, callback)


def configured_engine() -> str:
    return "chirp" if settings.SUBTITLE_ENGINE.strip().lower() == "chirp" else "gemini"


def transcribe_audio_chunks(chunks: list[dict], callback: ProgressCallback | None = None) -> tuple[str, str]:
    """Return ``(srt_text, subtitle_source)`` for ordered GCS audio chunks.

    Raises when every enabled engine fails.
    """

    if configured_engine() == "chirp":
        return _chirp(chunks, callback), CHIRP_SOURCE
    try:
        return _gemini(chunks, callback), GEMINI_SOURCE
    except Exception as gemini_error:
        if not settings.SUBTITLE_FALLBACK_TO_CHIRP:
            raise
        if callback:
            callback(
                "engine_fallback",
                "Gemini falhou; refazendo toda a transcrição com o Chirp 3",
                {
                    "from_engine": "gemini",
                    "to_engine": "chirp",
                    "error_type": type(gemini_error).__name__,
                    "error_message": str(gemini_error)[:2000],
                },
            )
        try:
            return _chirp(chunks, callback), CHIRP_SOURCE
        except Exception as chirp_error:
            raise RuntimeError(
                f"Falha na transcrição. Gemini: {type(gemini_error).__name__}: {gemini_error} | "
                f"Chirp 3: {type(chirp_error).__name__}: {chirp_error}"
            ) from chirp_error
