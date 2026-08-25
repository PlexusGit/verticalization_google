"""Media processing primitives for the Plexus Vertical Cuts transcode worker.

The module deliberately contains no HTTP concerns.  It downloads a GCS object,
probes it, creates a high-quality H.264/AAC rendition and 16 kHz mono FLAC
segments, uploads the results, and always removes its temporary workspace.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from google.cloud import storage

LOGGER = logging.getLogger(__name__)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FALSE_VALUES = {"0", "false", "no", "off"}


class TranscodeError(RuntimeError):
    """An expected media-processing failure safe to report to an API caller."""


@dataclass(frozen=True)
class Settings:
    """Runtime configuration loaded from environment variables."""

    bucket_name: str
    max_height: int = 1080
    audio_chunk_seconds: int = 600
    timeout_seconds: int = 10_800
    use_nvenc: bool = True
    temp_root: str = "/tmp"

    @classmethod
    def from_env(cls) -> "Settings":
        bucket = os.getenv("BUCKET_NAME", "").strip()
        if not bucket:
            raise ValueError("BUCKET_NAME must be configured")
        return cls(
            bucket_name=bucket,
            max_height=_positive_int("HIGH_RES_MAX_HEIGHT", 1080),
            audio_chunk_seconds=_positive_int("AUDIO_CHUNK_SECONDS", 600),
            timeout_seconds=_positive_int("TRANSCODE_TIMEOUT_SECONDS", 10_800),
            use_nvenc=os.getenv("USE_NVENC", "1").strip().lower() not in _FALSE_VALUES,
            temp_root=os.getenv("TRANSCODE_TEMP_ROOT", "/tmp"),
        )


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def gpu_available(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """Return whether an NVIDIA GPU and the NVENC encoder are both usable."""

    try:
        gpu = runner(["nvidia-smi"], capture_output=True, timeout=10, check=False)
        if gpu.returncode != 0:
            return False
        encoders = runner(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = (encoders.stdout or "") + (encoders.stderr or "")
        return encoders.returncode == 0 and "h264_nvenc" in output
    except (OSError, subprocess.SubprocessError):
        return False


def probe_media(path: str) -> dict[str, Any]:
    """Return duration and primary video dimensions reported by ffprobe."""

    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=width,height",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
        payload = json.loads(result.stdout)
        streams = payload.get("streams") or []
        stream = streams[0] if streams else {}
        duration = float((payload.get("format") or {}).get("duration") or 0)
        return {
            "duration_seconds": round(duration, 3),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
        }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise TranscodeError(f"unable to probe media: {exc}") from exc


def build_video_command(source: str, destination: str, max_height: int, nvenc: bool) -> list[str]:
    """Build a high-quality, web-compatible H.264/AAC ffmpeg command."""

    encoder = (
        ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
        if nvenc
        else ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    )
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", source,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:min({max_height}\\,ih)",
        *encoder,
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", destination,
    ]


def build_audio_command(source: str, pattern: str, chunk_seconds: int) -> list[str]:
    """Build an ffmpeg command producing lossless speech-recognition chunks."""

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", source,
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac",
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1", pattern,
    ]


def _run(command: Sequence[str], timeout: int, label: str) -> None:
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError(f"{label} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise TranscodeError(f"could not start {label}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown ffmpeg error")[-1500:]
        raise TranscodeError(f"{label} failed (exit {result.returncode}): {detail.strip()}")
    LOGGER.info("%s completed in %.1fs", label, time.monotonic() - started)


class TranscodeProcessor:
    """Coordinates GCS transfer, ffmpeg processing, metadata and cleanup."""

    def __init__(self, settings: Settings, storage_client: storage.Client | None = None) -> None:
        self.settings = settings
        self.storage_client = storage_client or storage.Client(
            project=os.getenv("GOOGLE_CLOUD_PROJECT") or None
        )
        self.nvenc = settings.use_nvenc and gpu_available()
        LOGGER.info("Video encoder selected: %s", "h264_nvenc" if self.nvenc else "libx264")

    def process(
        self,
        video_id: str,
        source_path: str,
        high_path: str | None = None,
        audio_prefix: str | None = None,
    ) -> dict[str, Any]:
        """Process one GCS object and return serializable output metadata."""

        if not _SAFE_ID.fullmatch(video_id or ""):
            raise ValueError("video_id must contain only letters, digits, '.', '_' or '-'")
        source_path = _validate_object_path(source_path, "gcs_source_path")
        high_path = _validate_object_path(
            high_path or f"{video_id}/{video_id}_high.mp4", "high_gcs_path"
        )
        audio_prefix = _validate_object_path(
            (audio_prefix or f"{video_id}/audio").rstrip("/"), "audio_gcs_prefix"
        )

        bucket = self.storage_client.bucket(self.settings.bucket_name)
        workdir = Path(tempfile.mkdtemp(prefix=f"plexus-transcode-{video_id}-", dir=self.settings.temp_root))
        source = workdir / "source"
        high = workdir / "high.mp4"
        audio_dir = workdir / "audio"
        audio_dir.mkdir()
        try:
            blob = bucket.blob(source_path)
            if not blob.exists():
                raise FileNotFoundError(f"GCS source object not found: {source_path}")
            blob.download_to_filename(str(source))
            source_metadata = probe_media(str(source))

            command = build_video_command(
                str(source), str(high), self.settings.max_height, self.nvenc
            )
            encoder_used = "h264_nvenc" if self.nvenc else "libx264"
            try:
                _run(command, self.settings.timeout_seconds, "NVENC video transcode" if self.nvenc else "CPU video transcode")
            except TranscodeError:
                if not self.nvenc:
                    raise
                LOGGER.warning("NVENC failed; retrying the rendition with libx264", exc_info=True)
                high.unlink(missing_ok=True)
                _run(
                    build_video_command(str(source), str(high), self.settings.max_height, False),
                    self.settings.timeout_seconds,
                    "CPU fallback video transcode",
                )
                encoder_used = "libx264"

            output_metadata = probe_media(str(high))
            bucket.blob(high_path).upload_from_filename(str(high), content_type="video/mp4")

            pattern = str(audio_dir / "chunk_%03d.flac")
            _run(
                build_audio_command(str(source), pattern, self.settings.audio_chunk_seconds),
                self.settings.timeout_seconds,
                "FLAC audio extraction",
            )
            chunks: list[dict[str, Any]] = []
            for index, chunk in enumerate(sorted(audio_dir.glob("chunk_*.flac"))):
                object_path = f"{audio_prefix}/chunk_{index:03d}.flac"
                bucket.blob(object_path).upload_from_filename(str(chunk), content_type="audio/flac")
                chunks.append({
                    "path": object_path,
                    "offset_seconds": index * self.settings.audio_chunk_seconds,
                })
            if not chunks:
                raise TranscodeError("source contains no extractable audio stream")

            return {
                "video_id": video_id,
                "high_gcs_path": high_path,
                "gcs_audio_chunks": chunks,
                "audio_chunk_seconds": self.settings.audio_chunk_seconds,
                "duration_seconds": output_metadata["duration_seconds"],
                "resolution": {
                    "width": output_metadata["width"],
                    "height": output_metadata["height"],
                },
                "source_resolution": {
                    "width": source_metadata["width"],
                    "height": source_metadata["height"],
                },
                "video_encoder": encoder_used,
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _validate_object_path(value: str, field: str) -> str:
    value = (value or "").strip().lstrip("/")
    if not value or value.startswith("gs://") or ".." in value.split("/"):
        raise ValueError(f"{field} must be a bucket-relative GCS object path")
    return value
