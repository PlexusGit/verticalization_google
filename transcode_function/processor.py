"""Media processing primitives for the Plexus Vertical Cuts transcode worker.

The module deliberately contains no HTTP concerns.  It downloads a GCS object,
probes it, creates a high-quality H.264/AAC rendition and 16 kHz mono MP3
speech segments, uploads the results, and always removes its temporary workspace.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import select
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
    audio_bitrate: str = "64k"
    timeout_seconds: int = 10_800
    use_nvenc: bool = True
    temp_root: str = "/tmp"
    # CPU encoder quality/speed trade-off (x264 only; NVENC keeps its own tuning).
    x264_preset: str = "medium"
    x264_crf: int = 18
    # When the source is already a web-compatible H.264, copy the video stream
    # instead of re-encoding it. Sources that don't qualify still re-encode.
    remux_if_compatible: bool = True
    # Largest keyframe interval (seconds) accepted for the remux path: chapter
    # trimming and frame analysis seek from the previous keyframe, so sparse
    # keyframes would make every seek decode tens of seconds of video.
    max_remux_keyframe_gap_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "Settings":
        bucket = os.getenv("BUCKET_NAME", "").strip()
        if not bucket:
            raise ValueError("BUCKET_NAME must be configured")
        return cls(
            bucket_name=bucket,
            max_height=_positive_int("HIGH_RES_MAX_HEIGHT", 1080),
            audio_chunk_seconds=_positive_int("AUDIO_CHUNK_SECONDS", 600),
            audio_bitrate=os.getenv("AUDIO_BITRATE", "64k").strip() or "64k",
            timeout_seconds=_positive_int("TRANSCODE_TIMEOUT_SECONDS", 10_800),
            use_nvenc=os.getenv("USE_NVENC", "1").strip().lower() not in _FALSE_VALUES,
            temp_root=os.getenv("TRANSCODE_TEMP_ROOT", "/tmp"),
            x264_preset=_x264_preset(),
            x264_crf=_x264_crf(),
            remux_if_compatible=os.getenv("TRANSCODE_REMUX", "1").strip().lower() not in _FALSE_VALUES,
            max_remux_keyframe_gap_seconds=_positive_float(
                "TRANSCODE_MAX_REMUX_KEYFRAME_GAP_SECONDS", 15.0
            ),
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


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


_X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
}


def _x264_preset() -> str:
    raw = os.getenv("TRANSCODE_PRESET", "medium").strip().lower()
    if raw not in _X264_PRESETS:
        LOGGER.warning("TRANSCODE_PRESET=%r is not a valid x264 preset; using 'medium'", raw)
        return "medium"
    return raw


def _x264_crf() -> int:
    raw = os.getenv("TRANSCODE_CRF", "18")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("TRANSCODE_CRF must be an integer") from exc
    if not 0 <= value <= 51:
        raise ValueError("TRANSCODE_CRF must be between 0 and 51")
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
    """Return duration, dimensions and codec details reported by ffprobe.

    The codec fields drive the remux decision: a source that is already a
    web-compatible H.264 does not need to be re-encoded.
    """

    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "format=duration:stream=width,height,codec_name,pix_fmt,has_b_frames,"
        "avg_frame_rate:stream_tags=rotate:stream_side_data=rotation",
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
            "codec": stream.get("codec_name") or "",
            "pix_fmt": stream.get("pix_fmt") or "",
            "has_b_frames": int(stream.get("has_b_frames") or 0),
            "avg_frame_rate": stream.get("avg_frame_rate") or "",
            "rotation": _rotation_degrees(stream),
        }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise TranscodeError(f"unable to probe media: {exc}") from exc


def _rotation_degrees(stream: dict) -> int:
    """Extract the display rotation (degrees) ffprobe reports, or 0."""

    for side_data in stream.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            try:
                return int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                pass
    rotate = (stream.get("tags") or {}).get("rotate")
    if rotate is not None:
        try:
            return int(float(rotate))
        except (TypeError, ValueError):
            pass
    return 0


def measure_max_keyframe_gap(path: str) -> float | None:
    """Return the largest gap (seconds) between consecutive keyframes, or None.

    A file with a single keyframe reports its whole duration as the gap, which
    the remux gate then rejects. Returns None when the layout cannot be
    measured; callers treat that as "re-encode".
    """

    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("unable to measure keyframe layout of %s: %s", path, exc)
        return None
    keyframe_times = []
    for line in result.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[-1]:
            try:
                keyframe_times.append(float(parts[0]))
            except ValueError:
                continue
    if len(keyframe_times) < 2:
        return float("inf")
    return max(b - a for a, b in zip(keyframe_times, keyframe_times[1:]))


def remux_blocker(info: dict[str, Any], settings: "Settings") -> str | None:
    """Return the reason a source must be re-encoded, or None when a remux works.

    Rotation is excluded on purpose: re-encoding bakes the display matrix into
    the pixels, while a stream copy would keep the rotate tag and confuse
    downstream frame analysis.
    """

    if not settings.remux_if_compatible:
        return "stream copy disabled (TRANSCODE_REMUX=0)"
    if (info.get("codec") or "").lower() != "h264":
        return f"video codec {info.get('codec')!r} needs re-encoding"
    if (info.get("pix_fmt") or "").lower() != "yuv420p":
        return f"pixel format {info.get('pix_fmt')!r} needs re-encoding"
    if int(info.get("height") or 0) > settings.max_height:
        return f"height {info.get('height')} exceeds the {settings.max_height}p cap"
    if int(info.get("rotation") or 0) != 0:
        return f"rotation of {info.get('rotation')}° needs re-encoding"
    return None


def build_video_command(
    source: str, destination: str, max_height: int, nvenc: bool,
    preset: str = "medium", crf: int = 18,
) -> list[str]:
    """Build a high-quality, web-compatible H.264/AAC ffmpeg command."""

    encoder = (
        ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
        if nvenc
        else ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]
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


def build_remux_command(source: str, destination: str) -> list[str]:
    """Copy the H.264 stream into a normalized MP4 without re-encoding it.

    The audio is still re-encoded to AAC so the rendition is identical in kind
    to the re-encode path; only the expensive video encode is skipped.
    """

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", source,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", destination,
    ]


def build_audio_command(
    source: str, pattern: str, chunk_seconds: int, segment_list: str, bitrate: str = "64k"
) -> list[str]:
    """Build an ffmpeg command producing small mono 16 kHz MP3 speech chunks.

    MP3 keeps a 10-minute chunk well under the inline request limit of the
    transcription API. The CSV segment list records each chunk's exact start and
    end, so offsets never depend on assuming every chunk is exactly N seconds.
    """

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", source,
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", bitrate,
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-segment_list", segment_list, "-segment_list_type", "csv",
        "-reset_timestamps", "1", pattern,
    ]


MIN_AUDIO_CHUNK_SECONDS = 0.5


def read_audio_segments(segment_list: str, directory: Path) -> list[tuple[Path, float, float]]:
    """Parse ffmpeg's CSV segment list into ``[(path, start, end)]`` sorted by start.

    Chunks shorter than half a second (an encoder tail) are dropped: they carry
    no usable speech and are rejected by the transcription API.
    """

    chunks: list[tuple[Path, float, float]] = []
    try:
        with open(segment_list, newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 3:
                    continue
                path = directory / os.path.basename(row[0])
                start, end = float(row[1]), float(row[2])
                if path.exists() and end - start >= MIN_AUDIO_CHUNK_SECONDS:
                    chunks.append((path, start, end))
    except (OSError, ValueError) as exc:
        raise TranscodeError(f"unable to read the audio segment list: {exc}") from exc
    return sorted(chunks, key=lambda chunk: chunk[1])


PROGRESS_LOG_INTERVAL_SECONDS = 15.0


def _with_progress(command: Sequence[str]) -> list[str]:
    """Insert ``-progress pipe:1 -nostats`` so ffmpeg reports out_time on stdout."""

    cmd = list(command)
    index = cmd.index("-i")
    return cmd[:index] + ["-progress", "pipe:1", "-nostats"] + cmd[index:]


def _run(
    command: Sequence[str],
    timeout: int,
    label: str,
    progress_total_seconds: float | None = None,
) -> None:
    """Run an ffmpeg step, logging start, periodic progress, and completion.

    When ``progress_total_seconds`` is given, ffmpeg's machine-readable
    progress stream is consumed so a long encode reports its percentage instead
    of looking hung in the logs.
    """

    started = time.monotonic()
    LOGGER.info("%s started", label)
    if progress_total_seconds:
        _run_with_progress(list(command), timeout, label, progress_total_seconds, started)
        return
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


def _run_with_progress(
    command: list[str], timeout: int, label: str, total_seconds: float, started: float
) -> None:
    try:
        process = subprocess.Popen(
            _with_progress(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError as exc:
        raise TranscodeError(f"could not start {label}: {exc}") from exc
    out_time = 0.0
    speed = ""
    last_log = started
    deadline = started + timeout
    timed_out = False
    try:
        while True:
            # select keeps the timeout enforced even if ffmpeg stalls silently.
            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if ready:
                line = process.stdout.readline()
                key, _, value = line.partition("=")
                if key == "out_time_us":
                    try:
                        out_time = float(value) / 1e6
                    except ValueError:
                        pass
                elif key == "speed":
                    speed = value.strip()
            now = time.monotonic()
            if out_time > 0 and now - last_log >= PROGRESS_LOG_INTERVAL_SECONDS:
                LOGGER.info(
                    "%s progress: %.0f%% (%.0fs elapsed%s)",
                    label,
                    min(100.0, out_time / total_seconds * 100),
                    now - started,
                    f", speed {speed}" if speed else "",
                )
                last_log = now
            if process.poll() is not None:
                for line in process.stdout:
                    key, _, value = line.partition("=")
                    if key == "out_time_us":
                        try:
                            out_time = float(value) / 1e6
                        except ValueError:
                            pass
                break
            if now > deadline:
                timed_out = True
                process.kill()
                process.wait()
                break
        stderr = process.stderr.read() if process.stderr else ""
        if timed_out:
            raise TranscodeError(f"{label} timed out after {timeout} seconds")
        if process.returncode:
            detail = (stderr or "unknown ffmpeg error")[-1500:]
            raise TranscodeError(f"{label} failed (exit {process.returncode}): {detail.strip()}")
    finally:
        if process.poll() is None:
            process.kill()
    LOGGER.info("%s completed in %.1fs", label, time.monotonic() - started)


class TranscodeProcessor:
    """Coordinates GCS transfer, ffmpeg processing, metadata and cleanup."""

    def __init__(self, settings: Settings, storage_client: storage.Client | None = None) -> None:
        self.settings = settings
        self.storage_client = storage_client or storage.Client(
            project=os.getenv("GOOGLE_CLOUD_PROJECT") or None
        )
        self.nvenc = settings.use_nvenc and gpu_available()
        LOGGER.info(
            "Video encoder for re-encodes: %s", "h264_nvenc" if self.nvenc else "libx264"
        )

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
            source_duration = source_metadata["duration_seconds"]

            blocker = remux_blocker(source_metadata, self.settings)
            keyframe_gap = None
            if blocker is None:
                keyframe_gap = measure_max_keyframe_gap(str(source))
                if keyframe_gap is None:
                    blocker = "keyframe layout could not be measured"
                elif keyframe_gap > self.settings.max_remux_keyframe_gap_seconds:
                    blocker = (
                        f"keyframe gap of {keyframe_gap:.1f}s exceeds "
                        f"{self.settings.max_remux_keyframe_gap_seconds:g}s"
                    )
            if blocker is None:
                LOGGER.info(
                    "Rendition for %s: remux (source is already H.264/yuv420p ≤%dp, "
                    "max keyframe gap %.1fs)",
                    video_id, self.settings.max_height, keyframe_gap,
                )
                _run(
                    build_remux_command(str(source), str(high)),
                    self.settings.timeout_seconds,
                    "Video remux",
                )
                encoder_used = "copy"
            else:
                LOGGER.info("Rendition for %s: re-encode (%s)", video_id, blocker)
                command = build_video_command(
                    str(source), str(high), self.settings.max_height, self.nvenc,
                    self.settings.x264_preset, self.settings.x264_crf,
                )
                encoder_used = "h264_nvenc" if self.nvenc else "libx264"
                try:
                    _run(
                        command, self.settings.timeout_seconds,
                        "NVENC video transcode" if self.nvenc else "CPU video transcode",
                        source_duration,
                    )
                except TranscodeError:
                    if not self.nvenc:
                        raise
                    LOGGER.warning("NVENC failed; retrying the rendition with libx264", exc_info=True)
                    high.unlink(missing_ok=True)
                    _run(
                        build_video_command(
                            str(source), str(high), self.settings.max_height, False,
                            self.settings.x264_preset, self.settings.x264_crf,
                        ),
                        self.settings.timeout_seconds,
                        "CPU fallback video transcode",
                        source_duration,
                    )
                    encoder_used = "libx264"

            output_metadata = probe_media(str(high))
            bucket.blob(high_path).upload_from_filename(str(high), content_type="video/mp4")

            pattern = str(audio_dir / "chunk_%03d.mp3")
            segment_list = str(workdir / "audio_segments.csv")
            _run(
                build_audio_command(
                    str(source), pattern, self.settings.audio_chunk_seconds,
                    segment_list, self.settings.audio_bitrate,
                ),
                self.settings.timeout_seconds,
                "MP3 audio extraction",
                source_duration,
            )
            chunks: list[dict[str, Any]] = []
            for index, (chunk, start, end) in enumerate(read_audio_segments(segment_list, audio_dir)):
                object_path = f"{audio_prefix}/chunk_{index:03d}.mp3"
                bucket.blob(object_path).upload_from_filename(str(chunk), content_type="audio/mpeg")
                chunks.append({
                    "path": object_path,
                    "offset_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
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
