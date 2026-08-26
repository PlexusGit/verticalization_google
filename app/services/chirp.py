"""PT-BR transcription with Speech-to-Text v2 Chirp 3.

The transcode worker stores short FLAC chunks in GCS. Processing them one at a
time keeps requests below service limits and lets this module add each chunk's
absolute offset when creating the canonical SRT.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import random
import time
from typing import Callable

from google.api_core import exceptions as google_exceptions
from google.api_core.client_options import ClientOptions
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech
from google.rpc import error_details_pb2

from app.config import settings
from app.services.srt import Cue, to_srt

logger = logging.getLogger("plexus.chirp")
ProgressCallback = Callable[[str, str, dict], None]
INITIAL_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 120.0
MAX_ATTEMPTS_PER_CHUNK = 6
POLL_INTERVAL_SECONDS = 30.0
OPERATION_TIMEOUT_SECONDS = 50 * 60


def _progress(callback: ProgressCallback | None, action: str, message: str, details: dict) -> None:
    logger.info("%s: %s | %s", action, message, details)
    if callback:
        callback(action, message, details)


@dataclass(frozen=True)
class AudioChunk:
    path: str
    offset_seconds: float = 0


def _seconds(duration) -> float:
    if duration is None:
        return 0
    seconds = float(getattr(duration, "seconds", 0) or 0)
    nanos = float(getattr(duration, "nanos", 0) or 0)
    return seconds + nanos / 1_000_000_000


def _error_details(error: Exception) -> dict:
    details = {
        "error_type": type(error).__name__,
        "status_code": getattr(error, "code", None),
        "error_message": str(error)[:2000],
    }
    quota_violations = []
    for item in getattr(error, "details", None) or []:
        if isinstance(item, error_details_pb2.ErrorInfo):
            details.update({
                "reason": item.reason,
                "domain": item.domain,
                "metadata": dict(item.metadata),
            })
        elif isinstance(item, error_details_pb2.QuotaFailure):
            quota_violations.extend(
                {"subject": violation.subject, "description": violation.description}
                for violation in item.violations
            )
        elif isinstance(item, error_details_pb2.RetryInfo):
            details["server_retry_delay_seconds"] = _seconds(item.retry_delay)
    if quota_violations:
        details["quota_violations"] = quota_violations
    return details


def _retry_delay(error: Exception, attempt: int) -> float:
    server_delay = float(_error_details(error).get("server_retry_delay_seconds", 0) or 0)
    exponential_delay = min(INITIAL_BACKOFF_SECONDS * 2 ** max(0, attempt - 1), MAX_BACKOFF_SECONDS)
    return max(server_delay, exponential_delay * random.uniform(0.8, 1.2))


def _client(location: str) -> SpeechClient:
    return SpeechClient(
        client_options=ClientOptions(
            api_endpoint=f"{location}-speech.googleapis.com",
            quota_project_id=settings.PROJECT_ID,
        )
    )


def _request(location: str, uri: str) -> cloud_speech.BatchRecognizeRequest:
    config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=["pt-BR"],
        model="chirp_3",
        features=cloud_speech.RecognitionFeatures(enable_word_time_offsets=True),
    )
    return cloud_speech.BatchRecognizeRequest(
        recognizer=f"projects/{settings.PROJECT_ID}/locations/{location}/recognizers/_",
        config=config,
        files=[cloud_speech.BatchRecognizeFileMetadata(uri=uri)],
        recognition_output_config=cloud_speech.RecognitionOutputConfig(
            inline_response_config=cloud_speech.InlineOutputConfig()
        ),
    )


def _wait_for_operation(operation, callback: ProgressCallback | None, chunk_index: int, location: str):
    operation_name = getattr(getattr(operation, "operation", None), "name", None)
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    poll_failures = 0
    if not getattr(getattr(operation, "operation", None), "done", False):
        time.sleep(POLL_INTERVAL_SECONDS)
    while True:
        try:
            done = operation.done(retry=None)
        except google_exceptions.ResourceExhausted as exc:
            poll_failures += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Chirp 3 excedeu {OPERATION_TIMEOUT_SECONDS:.0f}s") from exc
            delay = min(remaining, max(POLL_INTERVAL_SECONDS, _retry_delay(exc, poll_failures)))
            _progress(
                callback,
                "chirp_poll_resource_exhausted",
                f"Polling da operação Chirp 3 recebeu 429 em {location}; a mesma operação será consultada novamente",
                {
                    "chunk_index": chunk_index,
                    "location": location,
                    "operation_name": operation_name,
                    "poll_failure_count": poll_failures,
                    "retry_delay_seconds": round(delay, 2),
                    **_error_details(exc),
                },
            )
            time.sleep(delay)
            continue
        if done:
            return operation.result(timeout=0, retry=None, polling=None)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Chirp 3 excedeu {OPERATION_TIMEOUT_SECONDS:.0f}s")
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _recognize(uri: str, callback: ProgressCallback | None = None, chunk_index: int = 0):
    """Recognize one chunk with regional alternation and observable retries."""

    locations = [settings.CHIRP_LOCATION]
    if settings.CHIRP_FALLBACK_LOCATION not in locations:
        locations.append(settings.CHIRP_FALLBACK_LOCATION)
    schedule = [locations[index % len(locations)] for index in range(MAX_ATTEMPTS_PER_CHUNK)]
    last_error: Exception | None = None
    for attempt, location in enumerate(schedule, start=1):
        started = time.monotonic()
        operation_name = None
        _progress(
            callback,
            "chirp_request_started",
            f"Enviando trecho {chunk_index + 1} ao Chirp 3 em {location}",
            {
                "chunk_index": chunk_index,
                "location": location,
                "attempt": attempt,
                "model": "chirp_3",
                "api_version": "v2",
            },
        )
        try:
            operation = _client(location).batch_recognize(request=_request(location, uri))
            operation_name = getattr(getattr(operation, "operation", None), "name", None)
            response = _wait_for_operation(operation, callback, chunk_index, location)
            _progress(
                callback,
                "chirp_request_completed",
                f"Chirp 3 concluiu o trecho {chunk_index + 1}",
                {
                    "chunk_index": chunk_index,
                    "location": location,
                    "attempt": attempt,
                    "operation_name": operation_name,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                },
            )
            return response
        except google_exceptions.ResourceExhausted as exc:
            last_error = exc
            will_retry = attempt < len(schedule)
            delay = _retry_delay(exc, attempt) if will_retry else 0
            details = {
                "chunk_index": chunk_index,
                "location": location,
                "attempt": attempt,
                "operation_name": operation_name,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "retry_delay_seconds": round(delay, 2),
                "will_retry": will_retry,
                "next_location": schedule[attempt] if will_retry else None,
                "backoff_exponent": attempt - 1,
                **_error_details(exc),
            }
            _progress(
                callback,
                "chirp_resource_exhausted",
                f"Speech-to-Text retornou 429 em {location}; " + ("alternando região após espera" if will_retry else "tentativas encerradas"),
                details,
            )
            if will_retry:
                time.sleep(delay)
        except Exception as exc:
            _progress(
                callback,
                "chirp_request_failed",
                f"Speech-to-Text falhou em {location}",
                {
                    "chunk_index": chunk_index,
                    "location": location,
                    "attempt": attempt,
                    "operation_name": operation_name,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    **_error_details(exc),
                },
            )
            raise
    if last_error:
        _progress(
            callback,
            "chirp_request_failed",
            f"Chirp 3 não conseguiu transcrever o trecho {chunk_index + 1} após alternar regiões",
            {
                "chunk_index": chunk_index,
                "attempt_count": len(schedule),
                "locations": locations,
                **_error_details(last_error),
            },
        )
        raise last_error
    raise RuntimeError("Chirp 3 não retornou uma resposta")


def _result_cues(results, offset: float) -> list[Cue]:
    """Group timestamped words into readable static subtitle cues."""

    cues: list[Cue] = []
    for result in results:
        if not result.alternatives:
            continue
        words = list(result.alternatives[0].words or [])
        for index in range(0, len(words), 8):
            group = words[index : index + 8]
            if not group:
                continue
            start = _seconds(group[0].start_offset) + offset
            end = _seconds(group[-1].end_offset) + offset
            text = " ".join(word.word for word in group).strip()
            if text:
                cues.append(Cue(start, max(end, start + 0.4), text))
    return cues


def transcribe_chunks(chunks: list[dict], callback: ProgressCallback | None = None) -> str:
    """Transcribe ordered GCS chunks with durable progress callbacks."""

    if not chunks:
        raise ValueError("nenhum trecho de áudio foi fornecido")
    ordered = sorted(chunks, key=lambda item: float(item.get("offset_seconds", 0)))
    _progress(callback, "chirp_started", "Transcrição Chirp 3 iniciada", {"chunk_count": len(ordered)})
    cues: list[Cue] = []
    for index, raw in enumerate(ordered):
        chunk = AudioChunk(str(raw.get("path", "")), float(raw.get("offset_seconds", 0)))
        if not chunk.path:
            _progress(callback, "chirp_chunk_skipped", "Trecho sem caminho foi ignorado", {"chunk_index": index})
            continue
        uri = chunk.path if chunk.path.startswith("gs://") else f"gs://{settings.BUCKET_NAME}/{chunk.path}"
        response = _recognize(uri, callback, index)
        file_result = response.results.get(uri)
        if file_result is None:
            _progress(callback, "chirp_chunk_empty", "Chirp não retornou resultado para o trecho", {"chunk_index": index})
            continue
        error = getattr(file_result, "error", None)
        if error and getattr(error, "code", 0):
            raise RuntimeError(f"Chirp 3 falhou no trecho {index + 1}: {error.message}")
        chunk_cues = _result_cues(file_result.transcript.results, chunk.offset_seconds)
        cues.extend(chunk_cues)
        _progress(
            callback,
            "chirp_chunk_parsed",
            f"Legendas extraídas do trecho {index + 1}",
            {"chunk_index": index, "cue_count": len(chunk_cues), "offset_seconds": chunk.offset_seconds},
        )
    if not cues:
        raise RuntimeError("Chirp 3 não produziu legendas")
    result = to_srt(sorted(cues, key=lambda cue: cue.start))
    _progress(callback, "chirp_completed", "Transcrição Chirp 3 concluída", {"cue_count": len(cues), "characters": len(result)})
    return result
