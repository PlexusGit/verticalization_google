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
