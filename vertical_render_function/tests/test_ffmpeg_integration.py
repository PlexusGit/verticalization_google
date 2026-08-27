"""Small real-FFmpeg smoke test; skipped on machines without media tools."""
import dataclasses
import json
import shutil
import subprocess

import pytest

import renderer


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe are not installed",
)


@pytest.mark.parametrize("mode", ["center_crop", "split_stack"])
def test_real_render_has_vertical_dimensions(tmp_path, mode):
    source = tmp_path / "source.mp4"
    output = tmp_path / f"{mode}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=0.4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )
    spec = renderer.validate_request(
        {
            "source_gcs_path": "source.mp4",
            "target_gcs_path": "output.mp4",
            "mode": mode,
            "trim_start": 0,
            "trim_end": 0.3,
            "video_resolution": {"w": 320, "h": 180},
        }
    )
    if mode == "center_crop":
        points = tuple({"time": index * .003, "x": 80 + index % 2 * 160, "y": 90} for index in range(100))
        spec = dataclasses.replace(spec, automatic_focal_points=points)
    renderer.render(spec, str(source), str(output), timeout=60)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1080, 1920)


def test_real_render_with_logo_does_not_hang(tmp_path):
    """A logo overlay must not cause FFmpeg to hang.

    Regression: ``-loop 1`` on the image input created an infinite stream
    that prevented the filter graph from terminating.  Without ``-loop 1``,
    a single-frame image input is held by the overlay filter's
    ``eof_action=repeat`` for the entire video duration.
    """

    source = tmp_path / "source.mp4"
    logo = tmp_path / "logo.png"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=0.4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=100x50:d=0.1",
            "-frames:v", "1", str(logo),
        ],
        check=True,
    )
    spec = renderer.validate_request(
        {
            "source_gcs_path": "source.mp4",
            "target_gcs_path": "output.mp4",
            "mode": "center_crop",
            "trim_start": 0,
            "trim_end": 0.3,
            "video_resolution": {"w": 320, "h": 180},
            "logo": {"gcs_path": "logo.png", "position": "top-right", "width": 100, "opacity": 1},
        }
    )
    renderer.render(spec, str(source), str(output), logo_path=str(logo), timeout=60)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1080, 1920)


def test_real_render_with_logo_and_text_overlay(tmp_path):
    """Logo + text overlay + captions must complete in finite time."""

    source = tmp_path / "source.mp4"
    logo = tmp_path / "logo.png"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=0.4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=100x50:d=0.1",
            "-frames:v", "1", str(logo),
        ],
        check=True,
    )
    spec = renderer.validate_request(
        {
            "source_gcs_path": "source.mp4",
            "target_gcs_path": "output.mp4",
            "mode": "center_crop",
            "trim_start": 0,
            "trim_end": 0.3,
            "video_resolution": {"w": 320, "h": 180},
            "logo": {"gcs_path": "logo.png", "position": "top-right", "width": 100, "opacity": 1},
            "text_overlays": [{"text": "Teste de overlay", "position": "top_center", "size": 48, "color": "#FFFFFF", "opacity": 1}],
            "captions": [{"start": 0, "end": 0.3, "text": "Legendas"}],
            "caption_style": {"font": "DejaVu Sans", "size": 54, "color": "#FFFFFF", "outline_color": "#000000", "outline_width": 3, "position": "bottom"},
        }
    )
    renderer.render(spec, str(source), str(output), logo_path=str(logo), timeout=60)
    assert output.exists() and output.stat().st_size > 0
