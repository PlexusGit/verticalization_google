from pathlib import Path
from unittest.mock import patch

import pytest

import renderer


def payload(**updates):
    data = {
        "source_gcs_path": "gs://media/source.mp4",
        "output_gcs_path": "gs://media/output.mp4",
        "trim_start": 10,
        "trim_end": 20,
        "video_resolution": {"w": 1920, "h": 1080},
        "mode": "center_crop",
    }
    data.update(updates)
    return data


def test_validation_defaults_to_center_crop_and_rejects_unsafe_paths():
    spec = renderer.validate_request(payload())
    assert spec.mode == "center_crop"
    assert spec.duration == 10
    with pytest.raises(renderer.ValidationError, match="safe GCS"):
        renderer.validate_request(payload(source_gcs_path="../secret"))
    with pytest.raises(renderer.ValidationError, match="trim_end"):
        renderer.validate_request(payload(trim_end=10))


def test_focal_strategy_is_documented_center():
    assert renderer.select_automatic_focal_point(12.5, 1920, 1080) == (960, 540)


def test_rpp_easing_holds_then_cosine_interpolates():
    frames = ({"time": 0.0, "x": 0.0}, {"time": 10.0, "x": 100.0})
    assert renderer.eased_value(5, frames, "x", 50) == 0
    assert renderer.eased_value(9, frames, "x", 50) == pytest.approx(50)
    assert renderer.eased_value(10, frames, "x", 50) == 100


def test_split_stack_has_independent_clamped_filters_and_exact_output():
    spec = renderer.validate_request(payload(
        mode="split_stack",
        keyframes_top=[{"time": 10, "x": -200, "y": -10}],
        keyframes_bottom=[{"time": 10, "x": 9999, "y": 9999}],
    ))
    graph, label = renderer.build_video_filter(spec, None, None)
    assert "[vtop][vbottom]vstack=inputs=2" in graph
    assert graph.count("max(0,min(") == 4
    assert graph.count("scale=1080:960") == 2
    assert label == "v0"


def test_center_captions_logo_and_text_filter_chain(tmp_path):
    spec = renderer.validate_request(payload(
        captions=[{"start": 11, "end": 12, "text": "Olá"}],
        logo={"gcs_path": "gs://media/logo.png", "position": "top_right", "size": 120, "opacity": .5},
        text_overlays=[{"text": "AO VIVO", "position": "top_left", "size": 44, "color": "#FF0000", "opacity": .8}],
    ))
    ass = tmp_path / "captions.ass"
    renderer.write_ass_captions(spec, str(ass))
    assert "Dialogue: 0,0:00:01.00,0:00:02.00" in ass.read_text()
    graph, label = renderer.build_video_filter(spec, str(ass), 1)
    assert "crop=606:1080:657:0,scale=1080:1920" in graph
    assert "subtitles=" in graph and "colorchannelmixer=aa=0.5" in graph
    assert "drawtext=" in graph and label == "vtext0"


def test_render_uses_argument_array_and_checks_subprocess():
    spec = renderer.validate_request(payload())
    with patch("renderer.subprocess.run") as run:
        renderer.render(spec, "/tmp/in.mp4", "/tmp/out.mp4")
    command = run.call_args.args[0]
    assert isinstance(command, list)
    assert command[0] == "ffmpeg"
    assert "shell" not in run.call_args.kwargs
    assert run.call_args.kwargs["check"] is True
    assert command[-1] == "/tmp/out.mp4"
