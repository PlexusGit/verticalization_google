"""Tests for the v12 preset (YuNet detector, CPU) of the shared autoframe pipeline."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

import main
from autoframe_v10 import artifacts, models
from autoframe_v10.config import Config

pytest.importorskip("scipy", reason="scipy not installed")

KWARGS = dict(source_gcs_path="gs://b/s.mp4", source_generation="7",
              trim_start=10.0, trim_end=70.0, source_width=1920, source_height=1080)


@pytest.fixture
def app():
    from flask import Flask
    return Flask(__name__)


def cfg_for(engine, tmp_path, **kw):
    return Config.for_engine(engine, video="/tmp/src.mp4", workdir=str(tmp_path), **kw)


# ------------------------------------------------------------------- preset
def test_v12_preset_values_and_v10_defaults_unchanged(tmp_path):
    v12 = cfg_for("v12", tmp_path)
    assert (v12.engine, v12.face_detector, v12.face_stride, v12.face_conf) == ("v12", "yunet", 2, 0.6)
    assert v12.asd_durations == (1, 2, 4)
    assert (v12.interp, v12.encoder_preset) == ("cubic", "veryfast")
    assert v12.face_model.endswith("face_detection_yunet_2023mar.onnx") and Path(v12.face_model).exists()

    v10 = cfg_for("v10", tmp_path)
    assert (v10.engine, v10.face_detector, v10.face_stride, v10.face_conf) == ("v10", "yolo", 1, 0.5)
    assert v10.asd_durations == (1, 1, 1, 2, 2, 2, 3, 3, 4, 5, 6)
    assert (v10.interp, v10.encoder_preset) == ("lanczos4", "")
    with pytest.raises(ValueError):
        Config.for_engine("v99")


# ------------------------------------------------------------- fingerprints
def test_v12_fingerprint_differs_and_v10_fingerprint_is_pinned(tmp_path):
    v10 = artifacts.plan_fingerprint(cfg_for("v10", tmp_path), **KWARGS)
    v12 = artifacts.plan_fingerprint(cfg_for("v12", tmp_path), **KWARGS)
    assert v10 != v12
    assert artifacts.strategy_version(cfg_for("v10", tmp_path)) == 10
    assert artifacts.strategy_version(cfg_for("v12", tmp_path)) == 12
    # v12 reacts to its own tuning; stride is part of the plan identity.
    from dataclasses import replace
    assert v12 != artifacts.plan_fingerprint(replace(cfg_for("v12", tmp_path), face_stride=1), **KWARGS)
    assert v12 != artifacts.plan_fingerprint(replace(cfg_for("v12", tmp_path), asd_durations=(1, 2)), **KWARGS)
    # Added v12 fields must not leak into v10: only fields v10 already hashed matter.
    from dataclasses import replace as r
    assert v10 == artifacts.plan_fingerprint(r(cfg_for("v10", tmp_path), face_stride=1, interp="lanczos4"), **KWARGS)


def test_artifact_path_carries_the_strategy_version():
    assert artifacts.artifact_blob_path("v", "c", "f" * 64).startswith("v/chapters/c/autoframe/v10/")
    assert artifacts.artifact_blob_path("v", "c", "f" * 64, 12).startswith("v/chapters/c/autoframe/v12/")


# ----------------------------------------------------------------- tracking
def _dets(frames, present):
    return [[{"frame": f, "bbox": [100, 100, 200, 220], "conf": 0.9}] if f in present else [] for f in range(frames)]


def test_tracker_with_stride_keeps_shorter_detections_and_interpolates(tmp_path):
    from autoframe_v10.analyze import track_scene
    cfg = cfg_for("v12", tmp_path)                         # stride 2 -> min 5 detections
    dets = _dets(40, set(range(0, 40, 2)))                  # 20 detections on even frames
    tracks = track_scene(cfg, dets)
    assert len(tracks) == 1 and tracks[0]["frames"][0] == 0 and tracks[0]["frames"][-1] == 38
    assert len(tracks[0]["boxes"]) == 39                    # odd frames interpolated

    brief = _dets(40, {0, 2, 4})                            # 3 detections < 5
    assert track_scene(cfg, brief) == []
    # With stride 1 the original rule (> track_min_len) still applies.
    assert track_scene(cfg_for("v10", tmp_path), _dets(40, set(range(8)))) == []


def test_tracker_drops_a_lone_detection_instead_of_crashing(tmp_path):
    from autoframe_v10.analyze import track_scene
    cfg = cfg_for("v12", tmp_path, track_min_len=1)   # stride rule still needs >= 2 detections
    assert track_scene(cfg, _dets(5, {2})) == []
    assert len(track_scene(cfg, _dets(6, {2, 4}))) == 1


# ----------------------------------------------------------- yunet detector
def test_yunet_detection_uses_stride_and_scales_boxes_to_the_source(tmp_path, monkeypatch):
    from autoframe_v10 import analyze

    class FakeCap:
        def __init__(self, _path):
            self.n = 0
        def get(self, prop):
            import cv2
            return {cv2.CAP_PROP_FRAME_WIDTH: 1920, cv2.CAP_PROP_FRAME_HEIGHT: 1080}.get(prop, 0)
        def set(self, *_a):
            pass
        def read(self):
            return True, np.zeros((1080, 1920, 3), np.uint8)
        def release(self):
            pass

    calls = []

    class FakeDetector:
        def setInputSize(self, size):
            calls.append(("size", size))
        def detect(self, frame):
            calls.append(("detect", frame.shape[:2]))
            return None, np.array([[100.0, 50.0, 60.0, 80.0] + [0] * 10 + [0.9]], dtype=np.float32)

    monkeypatch.setattr(analyze.cv2, "VideoCapture", FakeCap)
    monkeypatch.setattr(analyze.models, "face_detector", lambda cfg: FakeDetector())
    cfg = cfg_for("v12", tmp_path)
    dets = analyze.detect_faces(cfg, 100, 106)

    assert len(dets) == 6
    assert [bool(d) for d in dets] == [True, False, True, False, True, False]   # stride 2
    assert [c for c in calls if c[0] == "detect"] == [("detect", (360, 640))] * 3   # downscaled to 640 wide
    box = dets[0][0]
    assert box["frame"] == 100 and box["conf"] == pytest.approx(0.9, abs=1e-6)
    assert box["bbox"] == pytest.approx([300.0, 150.0, 480.0, 390.0])               # x3 scale


def test_yunet_model_loads_and_finds_no_face_in_a_blank_frame(tmp_path):
    models.reset()
    detector = models.face_detector(cfg_for("v12", tmp_path))
    detector.setInputSize((640, 360))
    _, faces = detector.detect(np.zeros((360, 640, 3), np.uint8))
    assert faces is None or len(faces) == 0


# ------------------------------------------------------------------- render
def test_render_helpers_follow_the_preset(tmp_path):
    import cv2
    from autoframe_v10.render import interp_flag
    assert interp_flag(cfg_for("v12", tmp_path)) == cv2.INTER_CUBIC
    assert interp_flag(cfg_for("v10", tmp_path)) == cv2.INTER_LANCZOS4


def test_ffmpeg_writer_adds_the_x264_preset_only_when_configured(tmp_path, monkeypatch):
    from autoframe_v10 import render
    seen = []
    monkeypatch.setattr(render.subprocess, "Popen", lambda cmd, **k: seen.append(cmd) or Mock())
    render.ffmpeg_writer(tmp_path / "a.mp4", 1080, 1920, 25, cfg_for("v12", tmp_path))
    render.ffmpeg_writer(tmp_path / "b.mp4", 1080, 1920, 25, cfg_for("v10", tmp_path))
    assert seen[0][seen[0].index("-preset") + 1] == "veryfast"
    assert "-preset" not in seen[1]


# ------------------------------------------------------------------ routing
def _payload():
    return {
        "video_id": "video_1", "chapter_id": "chapter_1", "render_id": "render_1",
        "source_gcs_path": "gs://input/source.mp4", "output_gcs_path": "gs://output/render.mp4",
        "trim_start": 0, "trim_end": 5, "video_resolution": {"w": 1920, "h": 1080}, "mode": "center_crop",
    }


def _invoke(app, monkeypatch, engine, mode="center_crop"):
    from flask import request
    monkeypatch.setattr(main, "API_KEY", "key")
    monkeypatch.setattr(main, "AUTOFRAME_ENGINE", engine)
    monkeypatch.setattr(main, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(main, "_download", lambda *a, **k: None)
    monkeypatch.setattr(main, "_upload", lambda *a, **k: "gs://output/render.mp4")
    with app.test_request_context("/", method="POST", json={**_payload(), "mode": mode}, headers={"X-API-Key": "key"}):
        return main.vertical_render(request)


def test_v12_engine_routes_center_crop_with_strategy_version_12(app, monkeypatch):
    seen = {}

    def fake(*a, **k):
        seen["engine"] = k.get("engine")
        Path(a[2]).write_bytes(b"mp4")
        return {"camera_plan_gcs_path": "gs://b/plan.gz", "camera_plan_fingerprint": "ab" * 32}

    monkeypatch.setattr(main, "_render_with_v10", fake)
    monkeypatch.setattr(main, "render", Mock(side_effect=AssertionError("legacy render must not run")))
    body, status, _ = _invoke(app, monkeypatch, "v12")
    assert status == 200 and seen["engine"] == "v12"
    assert body["focal_strategy_version"] == 12 and body["automatic_focal_points"] == []


def test_v12_failure_falls_back_to_a_deterministic_center_crop(app, monkeypatch):
    monkeypatch.setattr(main, "_render_with_v10", Mock(side_effect=RuntimeError("boom")))
    rendered = {}

    def fake_render(spec, src, out, **k):
        rendered["points"] = spec.automatic_focal_points
        Path(out).write_bytes(b"mp4")

    monkeypatch.setattr(main, "render", fake_render)
    monkeypatch.setattr(main, "analyze_focal_points", Mock(side_effect=AssertionError("legacy detector must not run")))
    body, status, _ = _invoke(app, monkeypatch, "v12")
    assert status == 200 and rendered["points"] == () and body["focal_strategy_version"] == 12


def test_v12_ignores_split_stack(app, monkeypatch):
    monkeypatch.setattr(main, "_render_with_v10", Mock(side_effect=AssertionError("autoframe must not run")))
    monkeypatch.setattr(main, "analyze_focal_points", lambda *a, **k: ())
    monkeypatch.setattr(main, "render", lambda spec, src, out, **k: Path(out).write_bytes(b"mp4"))
    _body, status, _ = _invoke(app, monkeypatch, "v12", mode="split_stack")
    assert status == 200
