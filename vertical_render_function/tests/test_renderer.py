import dataclasses
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
    with pytest.raises(renderer.ValidationError, match="at most 241"):
        renderer.validate_request(payload(automatic_focal_points=[{"time": 10, "x": 1, "y": 1}] * 242))


def test_focal_strategy_falls_back_to_center_and_interpolates_faces():
    assert renderer.select_automatic_focal_point(12.5, 1920, 1080) == (960, 540)
    points = ({"time": 10, "x": 400, "y": 500}, {"time": 12, "x": 800, "y": 600})
    assert renderer.select_automatic_focal_point(12, 1920, 1080, points) == (800, 600)


def test_automatic_focal_points_create_dynamic_clamped_crop():
    spec = dataclasses.replace(
        renderer.validate_request(payload()),
        automatic_focal_points=(
            {"time": 10, "x": 400, "y": 540},
            {"time": 12, "x": 1400, "y": 540},
        ),
    )
    graph, _ = renderer.build_video_filter(spec, None, None)
    assert "crop=606:1080" in graph
    assert "max(0,min(1314" in graph
    assert "cos(PI*" in graph


def test_focal_trajectory_compaction_preserves_endpoints_and_large_changes():
    points = tuple({"time": index, "x": float(index), "y": 0.0} for index in range(100))
    points = points[:50] + ({"time": 50, "x": 500.0, "y": 0.0},) + points[51:]
    compacted = renderer.compact_keyframes(points)
    assert len(compacted) == renderer.MAX_EXPRESSION_KEYFRAMES
    assert compacted[0] == points[0]
    assert compacted[-1] == points[-1]
    assert {"time": 50, "x": 500.0, "y": 0.0} in compacted


def test_many_focal_points_do_not_generate_an_oversized_expression():
    points = tuple({"time": 10 + index, "x": 400 + index * 5, "y": 540} for index in range(100))
    spec = dataclasses.replace(renderer.validate_request(payload(trim_end=110)), automatic_focal_points=points)
    graph, _ = renderer.build_video_filter(spec, None, None)
    # One conditional per keyframe per axis, two axes, budget of 48 keyframes.
    assert graph.count("if(") <= 2 * renderer.MAX_EXPRESSION_KEYFRAMES


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


def test_template_geometry_uses_exact_logo_and_title_coordinates():
    spec = renderer.validate_request(payload(
        logo={"gcs_path": "gs://media/logo.png", "width": 270, "height": 150, "x": 730, "y": 96},
        text_overlays=[{"text": "Título", "x": 108, "y": 420, "size": 54}],
    ))
    graph, label = renderer.build_video_filter(spec, None, 1)
    assert "scale=270:150" in graph
    assert "overlay=730:96" in graph
    assert "x=108:y=420" in graph
    assert label == "vtext0"


def test_title_box_wraps_and_centers_text(tmp_path):
    spec = renderer.validate_request(payload(
        text_overlays=[{
            "text": "ENCONTRO RELAMPAGO DE EMERGENCIA COM TITULO MUITO LONGO QUE TRANSBORDARIA A CAIXA",
            "x": 108, "y": 346, "max_width": 864, "max_height": 230, "size": 54,
        }],
    ))
    graph, label = renderer.build_video_filter(spec, None, None, str(tmp_path))
    overlay_file = tmp_path / "overlay_0.txt"
    assert overlay_file.exists()
    wrapped = overlay_file.read_text()
    # Long titles are wrapped into real line breaks written to a text file —
    # drawtext's inline \n would render literal characters, not line feeds.
    assert "\n" in wrapped
    # The block is centered inside the 864x230 box at (108, 346).
    assert "x=108+(864-text_w)/2" in graph
    assert "y=346+(230-text_h)/2" in graph
    assert "text_align=C" in graph
    assert "line_spacing=" in graph
    if renderer._title_font_path():
        assert "fontfile=" in graph


def test_title_box_shrinks_font_until_text_fits(tmp_path):
    spec = renderer.validate_request(payload(
        text_overlays=[{
            "text": "UMA " * 60,
            "x": 0, "y": 0, "max_width": 300, "max_height": 120, "size": 80,
        }],
    ))
    graph, _ = renderer.build_video_filter(spec, None, None, str(tmp_path))
    import re
    size = int(re.search(r"fontsize=(\d+)", graph).group(1))
    assert size < 80


def test_title_box_hard_splits_oversized_words(tmp_path):
    spec = renderer.validate_request(payload(
        text_overlays=[{"text": "SUPERCALIFRAGILISTICOESPIALIDOSO" * 3, "x": 0, "y": 0, "max_width": 200, "size": 40}],
    ))
    renderer.build_video_filter(spec, None, None, str(tmp_path))
    lines = (tmp_path / "overlay_0.txt").read_text().split("\n")
    assert all(len(line) < 40 for line in lines)


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
