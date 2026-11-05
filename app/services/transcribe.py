"""Speech-to-text with Gemini Transcribe.

The transcode worker stores short mono 16 kHz MP3 chunks in GCS together with
each chunk's exact start offset. This module downloads those small files (never
the video), sends every chunk inline to the transcription model with word-level
timestamps (in parallel, with retries on transient errors), groups the words
into readable cues, shifts them by the chunk offset and builds one canonical SRT.

A chunk that still fails after its retries raises, so the SRT never has silent
holes; the caller (``app.services.subtitles``) decides whether to fall back.
"""
from __future__ import annotations

import datetime
import logging
import os
import random
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import settings
from app.services import storage
from app.services.srt import Cue, to_srt

logger = logging.getLogger("plexus.transcribe")
ProgressCallback = Callable[[str, str, dict], None]

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
INITIAL_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 60.0
AUDIO_MIME_TYPES = {".mp3": "audio/mp3", ".flac": "audio/flac", ".wav": "audio/wav"}

# Cue grouping limits.
MAX_CHARS_PER_CUE = 80
MAX_SECONDS_PER_CUE = 5.0
SENTENCE_MIN_CHARS = 35
MAX_PAUSE_SECONDS = 1.5
MIN_WORD_SECONDS = 0.3
MIN_CUE_SECONDS = 0.05

_sleep = time.sleep  # patched in tests


def _progress(callback: ProgressCallback | None, action: str, message: str, details: dict) -> None:
    logger.info("%s: %s | %s", action, message, details)
    if callback:
        callback(action, message, details)


def parse_offset(value) -> float:
    """Convert an SDK offset (``'12.34s'``, number, timedelta, ``HH:MM:SS.mmm``) to seconds."""

    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?[\d.]+s", text):
            return float(text[:-1])
        if ":" in text:
            parts = text.split(":")
            try:
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
            except ValueError:
                return 0.0
        match = re.search(r"[\d.]+", text)
        return float(match.group(0)) if match else 0.0
    total = getattr(value, "total_seconds", None)
    if callable(total):
        return float(total())
    return float(getattr(value, "seconds", 0) or 0) + float(getattr(value, "nanos", 0) or 0) / 1e9


def extract_words(response) -> list[tuple[str, float, float]]:
    """Return ``[(word, start, end)]`` in chunk-local seconds from a transcription response."""

    words: list[tuple[str, float, float]] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            transcription = getattr(part, "audio_transcription", None)
            if not transcription:
                continue
            for item in getattr(transcription, "words", None) or []:
                text = (getattr(item, "word", "") or "").strip()
                if not text:
                    continue
                start = parse_offset(getattr(item, "start_offset", None))
                end = parse_offset(getattr(item, "end_offset", None))
                if end <= start:
                    end = start + MIN_WORD_SECONDS
                words.append((text, start, end))
    return words


def group_cues(words: list[tuple[str, float, float]], offset: float) -> list[Cue]:
    """Group words into cues (<= ~80 chars / ~5 s, sentence and pause aware)."""

    cues: list[Cue] = []
    current: list[str] = []
    cue_start = cue_end = 0.0

    def flush() -> None:
        nonlocal current
        if current:
            cues.append(Cue(cue_start + offset, cue_end + offset, " ".join(current)))
        current = []

    for text, start, end in words:
        if current and start - cue_end > MAX_PAUSE_SECONDS:
            flush()
        if not current:
            cue_start = start
        current.append(text)
        cue_end = end
        joined = sum(len(item) for item in current) + len(current) - 1
        if (
            joined >= MAX_CHARS_PER_CUE
            or cue_end - cue_start >= MAX_SECONDS_PER_CUE
            or (text.endswith((".", "?", "!")) and joined >= SENTENCE_MIN_CHARS)
        ):
            flush()
    flush()
    return cues


def non_overlapping(cues: list[Cue]) -> list[Cue]:
    """Sort cues and trim each end so it never runs into the next cue."""

    ordered = sorted(cues, key=lambda cue: cue.start)
    result: list[Cue] = []
    for index, cue in enumerate(ordered):
        end = cue.end
        if index + 1 < len(ordered):
            end = min(end, max(ordered[index + 1].start, cue.start + MIN_CUE_SECONDS))
        result.append(Cue(cue.start, max(end, cue.start + MIN_CUE_SECONDS), cue.text))
    return result


def _response_text(response) -> str:
    try:
        return (response.text or "").strip()
    except Exception:
        return ""


def is_retryable(error: Exception) -> bool:
    if isinstance(error, genai_errors.APIError):
        return getattr(error, "code", None) in RETRYABLE_STATUS_CODES
    return isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError))


def _mime_type(path: str) -> str:
    return AUDIO_MIME_TYPES.get(os.path.splitext(path)[1].lower(), "audio/mp3")


def _language_codes() -> list[str]:
    return [code.strip() for code in settings.TRANSCRIBE_LANGUAGE_CODES.split(",") if code.strip()]


def _transcribe_chunk(
    client,
    config,
    path: str,
    offset: float,
    index: int,
    total: int,
    callback: ProgressCallback | None,
) -> list[Cue]:
    """Transcribe one chunk; retries transient failures with exponential backoff."""

    with open(path, "rb") as handle:
        audio = types.Part.from_bytes(data=handle.read(), mime_type=_mime_type(path))
    label = f"trecho {index + 1}/{total}"
    attempts = max(1, settings.TRANSCRIBE_MAX_ATTEMPTS)
    delay = INITIAL_BACKOFF_SECONDS
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        _progress(
            callback,
            "gemini_request_started",
            f"Enviando {label} ao {settings.TRANSCRIBE_MODEL}",
            {"chunk_index": index, "attempt": attempt, "model": settings.TRANSCRIBE_MODEL, "offset_seconds": offset},
        )
        try:
            response = client.models.generate_content(
                model=settings.TRANSCRIBE_MODEL, contents=[audio], config=config
            )
            words = extract_words(response)
            if words:
                cues = group_cues(words, offset)
                _progress(
                    callback,
                    "gemini_chunk_parsed",
                    f"Legendas extraídas do {label}",
                    {
                        "chunk_index": index,
                        "word_count": len(words),
                        "cue_count": len(cues),
                        "offset_seconds": offset,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    },
                )
                return cues
            if not _response_text(response):
                _progress(callback, "gemini_chunk_empty", f"Nenhuma fala detectada no {label}", {"chunk_index": index})
                return []
            last_error = RuntimeError("a resposta tem texto, mas não tem tempos por palavra")
        except Exception as exc:
            if not is_retryable(exc):
                _progress(
                    callback,
                    "gemini_request_failed",
                    f"Gemini falhou no {label}",
                    {"chunk_index": index, "attempt": attempt, "error_type": type(exc).__name__, "error_message": str(exc)[:2000]},
                )
                raise
            last_error = exc
        if attempt < attempts:
            wait = delay + random.uniform(0, delay / 4)
            _progress(
                callback,
                "gemini_request_retry",
                f"Tentativa {attempt}/{attempts} falhou no {label}; nova tentativa em {wait:.0f}s",
                {
                    "chunk_index": index,
                    "attempt": attempt,
                    "retry_delay_seconds": round(wait, 2),
                    "error_type": type(last_error).__name__,
                    "error_message": str(last_error)[:2000],
                },
            )
            _sleep(wait)
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)
    _progress(
        callback,
        "gemini_request_failed",
        f"Gemini não conseguiu transcrever o {label} após {attempts} tentativas",
        {"chunk_index": index, "attempt_count": attempts, "error_type": type(last_error).__name__, "error_message": str(last_error)[:2000]},
    )
    raise RuntimeError(
        f"Gemini falhou no {label} após {attempts} tentativas: {type(last_error).__name__}: {last_error}"
    )


def _object_path(path: str) -> str:
    return path.removeprefix(f"gs://{settings.BUCKET_NAME}/").lstrip("/")


def transcribe_chunks(chunks: list[dict], callback: ProgressCallback | None = None) -> str:
    """Transcribe ordered GCS audio chunks into one SRT (canonical video timeline).

    ``chunks`` is ``[{"path": ..., "offset_seconds": ...}, ...]``. Raises when a
    chunk cannot be fetched or transcribed, or when no speech was found at all.
    """

    if not chunks:
        raise ValueError("nenhum trecho de áudio foi fornecido")
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não configurada")
    ordered = sorted(
        (chunk for chunk in chunks if chunk.get("path")), key=lambda item: float(item.get("offset_seconds", 0))
    )
    if not ordered:
        raise ValueError("nenhum trecho de áudio com caminho válido foi fornecido")
    started = time.monotonic()
    _progress(
        callback,
        "gemini_started",
        "Transcrição Gemini iniciada",
        {"chunk_count": len(ordered), "model": settings.TRANSCRIBE_MODEL, "language_codes": _language_codes()},
    )
    work_dir = tempfile.mkdtemp(prefix="gemini-transcribe-")
    try:
        bucket = storage.bucket()
        local: list[tuple[str, float]] = []
        for index, chunk in enumerate(ordered):
            object_path = _object_path(str(chunk["path"]))
            destination = os.path.join(work_dir, f"chunk_{index:03d}{os.path.splitext(object_path)[1] or '.mp3'}")
            bucket.blob(object_path).download_to_filename(destination)
            local.append((destination, float(chunk.get("offset_seconds", 0))))
        megabytes = sum(os.path.getsize(path) for path, _ in local) / (1024 * 1024)
        _progress(
            callback,
            "gemini_chunks_downloaded",
            f"{len(local)} trecho(s) de áudio baixados ({megabytes:.1f} MB)",
            {"chunk_count": len(local), "megabytes": round(megabytes, 2)},
        )

        client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=settings.TRANSCRIBE_REQUEST_TIMEOUT_SECONDS * 1000),
        )
        config = types.GenerateContentConfig(
            audio_transcription_config=types.AudioTranscriptionConfig(
                word_timestamp=True,
                language_codes=_language_codes(),
            )
        )
        workers = max(1, min(settings.TRANSCRIBE_CONCURRENCY, len(local)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_transcribe_chunk, client, config, path, offset, index, len(local), callback)
                for index, (path, offset) in enumerate(local)
            ]
            try:
                cues = [cue for future in futures for cue in future.result()]
            except Exception:
                for future in futures:
                    future.cancel()
                raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if not cues:
        raise RuntimeError("Gemini não produziu legendas (nenhuma fala detectada)")
    result = to_srt(non_overlapping(cues))
    _progress(
        callback,
        "gemini_completed",
        "Transcrição Gemini concluída",
        {"cue_count": len(cues), "characters": len(result), "elapsed_seconds": round(time.monotonic() - started, 2)},
    )
    return result
