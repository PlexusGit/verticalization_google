"""Tests for the v10 autoframe pipeline (autoframe_v10).

Pure-numpy planner/compositor tests always run; tests that need scipy are
skipped when it is absent, and tests that need the ML models (ultralytics,
torch, mediapipe) are skipped likewise so the suite stays runnable in lean
environments.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

import main
from autoframe_v10 import artifacts
from autoframe_v10.config import Config
from autoframe_v10.render import compose, render_plan

scipy = pytest.importorskip("scipy", reason="scipy not installed")  # noqa: F401 — plan.py needs it
from autoframe_v10 import plan as v10plan  # noqa: E402


# ----------------------------------------------------------------- fixtures
FPS, W, H = 30, 1920, 1080


@pytest.fixture
def app():
    from flask import Flask
    return Flask(__name__)


def make_cfg(tmp_path, **overrides):
    base = dict(video="/tmp/src.mp4", start=0.0, duration=0.0, workdir=str(tmp_path), encoder="libx264")
    base.update(overrides)
    return Config(**base)


def make_track(tid, frames, cx, cy, w=200, h=240, speak=0.9, scene=0):
    n = len(frames)
    cx = np.asarray(cx, dtype=float)
    cy = np.asarray(cy, dtype=float)
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    speak = np.broadcast_to(np.asarray(speak, dtype=float), n).tolist()
    return {"id": tid, "scene": scene, "frames": list(frames), "boxes": boxes.tolist(), "speak": speak}


def make_analysis(tracks, *, n=90, scenes=None, start=0, fps=FPS, w=W, h=H):
    return {
        "fps": fps, "width": w, "height": h,
        "start_frame": start, "end_frame": start + n,
        "scenes": scenes if scenes is not None else [[start, start + n]],
        "tracks": tracks,
        "vad": [1] * n, "audio_rms": [0.1] * n,
    }


# ----------------------------------------------------------------- artifacts
def test_plan_fingerprint_stable_and_sensitive(tmp_path):
    cfg = make_cfg(tmp_path)
    kwargs = dict(source_gcs_path="gs://b/s.mp4", source_generation="7",
                  trim_start=10.0, trim_end=70.0, source_width=1920, source_height=1080)
    fp1 = artifacts.plan_fingerprint(cfg, **kwargs)
    assert fp1 == artifacts.plan_fingerprint(cfg, **kwargs)
    assert fp1 != artifacts.plan_fingerprint(cfg, **{**kwargs, "trim_end": 90.0})
    assert fp1 != artifacts.plan_fingerprint(cfg, **{**kwargs, "source_generation": "8"})
    assert fp1 != artifacts.plan_fingerprint(cfg, **{**kwargs, "source_gcs_path": "gs://b/other.mp4"})
    cfg_zoom = make_cfg(tmp_path, zoom_factor=4.0)
    assert fp1 != artifacts.plan_fingerprint(cfg_zoom, **kwargs)


def test_plan_roundtrip_and_validation(tmp_path):
    plan = {"format": artifacts.PLAN_FORMAT, "fingerprint": "ab" * 32,
            "frames": [{"layout": "single", "crop": [0, 0, 100, 178], "track": 0, "mode": "static", "scene": 0}],
            "fps": FPS, "width": W, "height": H, "start_frame": 0, "end_frame": 1, "segments": []}
    restored = artifacts.loads_plan(artifacts.dumps_plan(plan))
    assert restored == plan
    with pytest.raises(Exception):
        artifacts.loads_plan(b"not gzip")
    bad = dict(plan, format="other/9")
    with pytest.raises(Exception):
        artifacts.loads_plan(artifacts.dumps_plan(bad))


def test_artifact_blob_path():
    p = artifacts.artifact_blob_path("v_abc", "chapter_001", "f" * 64)
    assert p == f"v_abc/chapters/chapter_001/autoframe/v10/{'f' * 64}.plan.json.gz"


# ----------------------------------------------------------------- planning
def test_single_speaker_static_segment(tmp_path):
    cfg = make_cfg(tmp_path)
    frames = list(range(0, 90))
    track = make_track(0, frames, cx=np.full(90, 700.0), cy=np.full(90, 400.0))
    out = v10plan.plan(cfg, make_analysis([track], n=90))
    assert len(out["frames"]) == 90
    assert all(fr["layout"] == "single" and fr["track"] == 0 for fr in out["frames"])
    assert all(fr["mode"] == "static" for fr in out["frames"])
    # Face center sits at headroom position inside the crop.
    fr0 = out["frames"][0]
    x, y, cw, ch = fr0["crop"]
    assert abs((x + cw / 2) - 700) < 2
    assert abs((y + ch * cfg.headroom) - 400) < 2
    assert abs(cw / ch - cfg.output_w / cfg.output_h) < 0.02
    # plan.json is written to workdir
    assert (tmp_path / f"{cfg.tag}_plan.json").exists()


def test_distant_speaker_switch_is_cut(tmp_path):
    cfg = make_cfg(tmp_path)
    n = 180  # 6s: speaker A for 3s, then B talks
    frames = list(range(n))
    speak = [0.9] * 90 + [0.0] * 90
    a = make_track(0, frames, cx=np.full(n, 400.0), cy=np.full(n, 400.0), speak=speak)
    speak_b = [0.0] * 90 + [0.95] * 90
    b = make_track(1, frames, cx=np.full(n, 1600.0), cy=np.full(n, 400.0), speak=speak_b)
    out = v10plan.plan(cfg, make_analysis([a, b], n=n))
    targets = [fr["track"] for fr in out["frames"]]
    assert targets[0] == 0 and targets[-1] == 1
    switch_at = next(i for i, t in enumerate(targets) if t == 1)
    # min_shot_sec=1.5 & confirm_sec=0.4 -> switch lands a bit after 3s.
    assert 90 <= switch_at <= 90 + int(FPS * (cfg.confirm_sec + 0.4))
    seg = [s for s in out["segments"] if s["track"] == 1][0]
    assert seg["trans"] == "cut"  # faces far apart -> hard cut


def test_no_faces_centers_crop(tmp_path):
    cfg = make_cfg(tmp_path)
    out = v10plan.plan(cfg, make_analysis([], n=60))
    assert all(fr["layout"] == "center" and fr["mode"] == "nobody" for fr in out["frames"])
    x, y, cw, ch = out["frames"][0]["crop"]
    assert abs(x + cw / 2 - W / 2) < 1 and abs(y + ch / 2 - H / 2) < 1


def test_moving_face_tracks_within_bounds(tmp_path):
    cfg = make_cfg(tmp_path)
    n = 120
    frames = list(range(n))
    cx = np.linspace(300, 1600, n)  # fast drift -> track mode
    cy = np.full(n, 500.0)
    track = make_track(0, frames, cx=cx, cy=cy)
    out = v10plan.plan(cfg, make_analysis([track], n=n))
    for i, fr in enumerate(out["frames"]):
        x, y, cw, ch = fr["crop"]
        face_cx, face_cy = cx[i], cy[i]
        assert x - 1 <= face_cx <= x + cw + 1      # face stays inside the crop
        assert y - 1 <= face_cy <= y + ch + 1
        assert 0 <= x <= W - cw and 0 <= y <= H - ch  # crop stays in frame


def test_remove_short_runs():
    t = np.array([0] * 30 + [1] * 3 + [0] * 30)
    out = v10plan.remove_short_runs(t, min_run=10)
    assert (out == 0).all()
    t2 = np.array([0] * 30 + [1] * 40 + [0] * 30)
    out2 = v10plan.remove_short_runs(t2, min_run=10)
    assert (out2[30:70] == 1).all()


def test_crop_size_never_exceeds_frame(tmp_path):
    cfg = make_cfg(tmp_path)
    cw, ch = v10plan.crop_size_for(300, W, H, 1080, 1920, cfg)
    assert cw <= W and ch <= H and abs(cw / ch - 1080 / 1920) < 0.01
    cw2, ch2 = v10plan.crop_size_for(2000, W, H, 1080, 1920, cfg)  # huge face
    assert ch2 <= H and cw2 <= W
    cw3, ch3 = v10plan.crop_size_for(50, W, H, 1080, 1920, cfg)    # tiny face -> min_crop_h_ratio
    assert ch3 >= cfg.min_crop_h_ratio * H - 1


# ----------------------------------------------------------------- tracking helpers
def test_track_scene_gaps_and_min_len(tmp_path):
    cfg = make_cfg(tmp_path)
    # Face present frames 0-50 except a gap at 20-25 (< track_max_gap=10)
    dets = []
    for f in range(60):
        if 20 <= f <= 25:
            dets.append([])
        else:
            dets.append([{"frame": f, "bbox": [100, 100, 200, 220], "conf": 0.9}])
    from autoframe_v10.analyze import track_scene
    tracks = track_scene(cfg, dets)
    assert len(tracks) == 1
    assert tracks[0]["frames"][0] == 0 and tracks[0]["frames"][-1] == 59
    assert len(tracks[0]["boxes"]) == 60  # gap frames interpolated

    short = dets[:5] + [[] for _ in range(55)]  # 5 frames < track_min_len
    assert track_scene(cfg, short) == []


def test_iou_and_sigmoid():
    from autoframe_v10.analyze import iou, sigmoid
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == pytest.approx(1.0)
    assert iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(50 / 150)
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert sigmoid(0) == pytest.approx(0.5)


def test_audio_vad_thresholds(tmp_path):
    from autoframe_v10.analyze import audio_vad
    cfg = make_cfg(tmp_path)
    sr = 16000
    n = 90
    audio = np.zeros(sr * n // FPS, dtype=np.float32)
    mid = len(audio) // 2
    audio[mid:mid + sr] = 20000  # 1s of loud audio
    rms, vad = audio_vad(audio, sr, FPS, n, cfg)
    assert vad.any() and not vad.all()
    assert vad[mid * FPS // sr]  # voiced region flagged


# ----------------------------------------------------------------- compositor
def _frame(w=640, h=360, color=(10, 60, 200)):
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = color
    return img


def test_compose_single_center_blur(tmp_path):
    cfg = make_cfg(tmp_path)
    frame = _frame()
    fr = {"layout": "single", "crop": [100, 50, 200, 300]}
    out = compose(frame, fr, cfg)
    assert out.shape == (cfg.output_h, cfg.output_w, 3)
    out = compose(frame, {"layout": "center", "crop": [100, 50, 200, 300]}, cfg)
    assert out.shape == (cfg.output_h, cfg.output_w, 3)
    out = compose(frame, {"layout": "blur", "crop": [0, 0, 10, 10]}, cfg)
    assert out.shape == (cfg.output_h, cfg.output_w, 3)
    out = compose(frame, {"layout": "split", "crop": [0, 0, 100, 100],
                          "crop_top": [0, 0, 200, 300], "crop_bottom": [400, 0, 200, 300]}, cfg)
    assert out.shape == (cfg.output_h, cfg.output_w, 3)


def _write_source(tmp_path, seconds=2, fps=10, w=320, h=180, with_audio=True):
    import subprocess
    src = tmp_path / "src.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={seconds}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(src)]
    subprocess.run(cmd, check=True)
    return src


def _probe(path, *args):
    import subprocess
    return subprocess.run(
        ["ffprobe", "-v", "error", *args, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()


def test_render_plan_end_to_end(tmp_path):
    src = _write_source(tmp_path)
    cfg = make_cfg(tmp_path, video=str(src))
    n = 20
    frames = [{"layout": "single", "crop": [40, 20, 90, 160], "track": 0, "mode": "static", "scene": 0}
              for _ in range(n)]
    plan = {"fps": 10, "width": 320, "height": 180, "start_frame": 0, "end_frame": n,
            "frames": frames, "segments": []}
    out = tmp_path / "out.mp4"
    stats = render_plan(cfg, plan, str(out))
    assert stats["frames"] == n and stats["encoder"] == "libx264"
    assert _probe(out, "-select_streams", "v", "-show_entries", "stream=width,height,pix_fmt") == "1080,1920,yuv420p"
    duration = float(_probe(out, "-show_entries", "format=duration"))
    assert abs(duration - n / 10) < 0.2
    assert "aac" in _probe(out, "-select_streams", "a", "-show_entries", "stream=codec_name")


def test_render_plan_applies_overlay_builder(tmp_path):
    src = _write_source(tmp_path)
    cfg = make_cfg(tmp_path, video=str(src))
    n = 10
    frames = [{"layout": "single", "crop": [40, 20, 90, 160], "track": 0, "mode": "static", "scene": 0}
              for _ in range(n)]
    plan = {"fps": 10, "width": 320, "height": 180, "start_frame": 0, "end_frame": n,
            "frames": frames, "segments": []}
    calls = []

    def overlay_builder(input_label, logo_index):
        calls.append((input_label, logo_index))
        return [f"[{input_label}]negate[vneg]"], "vneg"

    out = tmp_path / "neg.mp4"
    render_plan(cfg, plan, str(out), overlay_builder=overlay_builder)
    assert calls == [("0:v", None)]
    # negate on testsrc produces non-zero variance; just verify a valid file.
    assert _probe(out, "-select_streams", "v", "-show_entries", "stream=width") == "1080"


def test_render_plan_encoder_fallback(tmp_path, monkeypatch):
    src = _write_source(tmp_path, seconds=1)
    cfg = make_cfg(tmp_path, video=str(src), encoder="h264_nvenc")  # unavailable on this machine
    n = 5
    plan = {"fps": 10, "width": 320, "height": 180, "start_frame": 0, "end_frame": n,
            "frames": [{"layout": "single", "crop": [0, 0, 90, 160], "track": 0, "mode": "static", "scene": 0}] * n,
            "segments": []}
    out = tmp_path / "fb.mp4"
    stats = render_plan(cfg, plan, str(out))
    assert stats["encoder"] == "libx264"
    assert out.exists() and out.stat().st_size > 0


def test_render_plan_no_audio_source(tmp_path):
    src = _write_source(tmp_path, seconds=1, with_audio=False)
    cfg = make_cfg(tmp_path, video=str(src))
    n = 10
    plan = {"fps": 10, "width": 320, "height": 180, "start_frame": 0, "end_frame": n,
            "frames": [{"layout": "single", "crop": [0, 0, 90, 160], "track": 0, "mode": "static", "scene": 0}] * n,
            "segments": []}
    out = tmp_path / "noaudio.mp4"
    render_plan(cfg, plan, str(out))
    assert _probe(out, "-select_streams", "v", "-show_entries", "stream=width") == "1080"


# ----------------------------------------------------------------- worker routing
def _payload(**over):
    p = {
        "video_id": "video_1", "chapter_id": "chapter_1", "render_id": "render_1",
        "source_gcs_path": "gs://input/source.mp4", "output_gcs_path": "gs://output/render.mp4",
        "trim_start": 0, "trim_end": 5, "video_resolution": {"w": 1920, "h": 1080},
        "mode": "center_crop",
    }
    p.update(over)
    return p


def _invoke(app, monkeypatch, engine="v10", **payload_overrides):
    from flask import request
    monkeypatch.setattr(main, "API_KEY", "key")
    monkeypatch.setattr(main, "AUTOFRAME_ENGINE", engine)
    monkeypatch.setattr(main, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(main, "_download", lambda *a, **k: None)
    monkeypatch.setattr(main, "_upload", lambda *a, **k: "gs://output/render.mp4")
    with app.test_request_context("/", method="POST", json=_payload(**payload_overrides), headers={"X-API-Key": "key"}):
        return main.vertical_render(request)


def test_v10_engine_routes_center_crop(app, monkeypatch):
    called = {}

    def fake_v10(*a, **k):
        called["v10"] = True
        Path(a[2]).write_bytes(b"mp4")   # output_path arg
        return {"camera_plan_gcs_path": "gs://b/plan.gz", "camera_plan_fingerprint": "ab" * 32}

    monkeypatch.setattr(main, "_render_with_v10", fake_v10)
    monkeypatch.setattr(main, "render", Mock(side_effect=AssertionError("legacy render must not run")))
    body, status, _ = _invoke(app, monkeypatch)
    assert status == 200 and called["v10"]
    assert body["focal_strategy_version"] == 10
    assert body["camera_plan_gcs_path"] == "gs://b/plan.gz"
    assert body["automatic_focal_points"] == []


def test_v10_failure_falls_back_to_center_crop(app, monkeypatch):
    monkeypatch.setattr(main, "_render_with_v10", Mock(side_effect=RuntimeError("boom")))
    captured = {}

    def fake_render(spec, input_path, output_path, **kw):
        captured["points"] = spec.automatic_focal_points
        Path(output_path).write_bytes(b"mp4")

    monkeypatch.setattr(main, "render", fake_render)
    body, status, _ = _invoke(app, monkeypatch)
    assert status == 200
    assert captured["points"] == ()          # deterministic central crop, not stale v3 points
    assert body["focal_strategy_version"] == 10
    assert "camera_plan_gcs_path" not in body


def test_v10_engine_ignores_split_stack(app, monkeypatch):
    monkeypatch.setattr(main, "_render_with_v10", Mock(side_effect=AssertionError("must not run")))
    seen = {}

    def fake_render(spec, input_path, output_path, **kw):
        seen["mode"] = spec.mode
        Path(output_path).write_bytes(b"mp4")

    monkeypatch.setattr(main, "render", fake_render)
    body, status, _ = _invoke(app, monkeypatch, mode="split_stack")
    assert status == 200 and seen["mode"] == "split_stack"
    assert body["focal_strategy_version"] == 3


def test_legacy_engine_uses_focal_pipeline(app, monkeypatch):
    monkeypatch.setattr(main, "_render_with_v10", Mock(side_effect=AssertionError("must not run")))
    monkeypatch.setattr(main, "analyze_focal_points", lambda *a: ({"time": 0.0, "x": 1.0, "y": 2.0},))

    def fake_render(spec, input_path, output_path, **kw):
        assert spec.automatic_focal_points
        Path(output_path).write_bytes(b"mp4")

    monkeypatch.setattr(main, "render", fake_render)
    body, status, _ = _invoke(app, monkeypatch, engine="legacy")
    assert status == 200 and body["focal_strategy_version"] == 3
    assert body["automatic_focal_points"]


def test_camera_plan_request_fields_validated():
    spec = main.validate_request(_payload(
        camera_plan_gcs_path="gs://b/v/c/plan.plan.json.gz",
        camera_plan_fingerprint="ab" * 32))
    assert spec.camera_plan_gcs_path == "gs://b/v/c/plan.plan.json.gz"
    assert spec.camera_plan_fingerprint == "ab" * 32
    with pytest.raises(Exception):
        main.validate_request(_payload(camera_plan_fingerprint="not hex!"))
    with pytest.raises(Exception):
        main.validate_request(_payload(camera_plan_gcs_path="../escape"))
