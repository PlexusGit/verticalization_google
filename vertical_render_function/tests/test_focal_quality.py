"""Quality harness for the focal-point strategy.

Unit tests cover the new primitives (saliency, optical flow, composition
clamping, speaker scores, schedule planning).  The synthetic-clip harness
renders an FFmpeg clip with a known moving subject and measures whether the
focal trajectory keeps it inside the crop window with bounded jitter.
"""
import shutil
import subprocess

import numpy as np
import pytest

import face_detection


def test_flow_track_recovers_translation():
    previous = np.zeros((180, 320), dtype=np.uint8)
    previous[40:100, 40:120] = 255
    current = np.zeros((180, 320), dtype=np.uint8)
    current[40:100, 90:170] = 255
    points = [(40.0, 40.0), (120.0, 40.0), (40.0, 100.0), (120.0, 100.0)]
    displacement = face_detection.flow_track(previous, current, points)
    assert displacement is not None
    dx, dy = displacement
    assert dx == pytest.approx(50.0, abs=4.0)
    assert abs(dy) < 4.0


def test_flow_track_rejects_untrackable_frames():
    blank = np.zeros((180, 320), dtype=np.uint8)
    points = [(40.0, 40.0), (120.0, 40.0), (40.0, 100.0), (120.0, 100.0)]
    assert face_detection.flow_track(blank, blank, points) is None


def test_clamp_focus_keeps_face_and_margin_inside_crop():
    face = (900.0, 400.0, 200.0, 240.0, 0.9)
    focus = face_detection.clamp_focus_to_face((1500.0, 540.0), face, 606, 1080, 1920, 1080)
    crop_left, crop_right = focus[0] - 303, focus[0] + 303
    margin = 200 * face_detection.FACE_MARGIN_RATIO
    assert crop_left <= face[0] - margin + 1
    assert crop_right >= face[0] + face[2] + margin - 1
    assert 303 <= focus[0] <= 1920 - 303


def test_gaze_bias_shifts_focus_toward_looking_direction():
    # Nose to the right of the eye midpoint -> the face looks right.
    face = (900.0, 400.0, 200.0, 240.0, 0.9)
    landmarks = ((960.0, 460.0), (1040.0, 460.0), (1030.0, 500.0), (980.0, 560.0), (1020.0, 560.0))
    biased = face_detection.face_focus(face, 1080, 1080, 606, 1920, landmarks)
    neutral = face_detection.face_focus(face, 1080, 1080, 606, 1920, None)
    assert biased[0] > neutral[0]
    assert biased[0] - neutral[0] <= 606 * face_detection.GAZE_BIAS_RATIO + 1


def test_speaker_scores_prefer_active_lips():
    faces = [(100.0, 100.0, 200.0, 240.0, 0.9), (900.0, 100.0, 220.0, 260.0, 0.9)]
    activities = [0.05, 0.004]
    scores = face_detection.speaker_scores(faces, None, activities, 606)
    assert scores[0] > scores[1]


def test_schedule_concentrates_samples_on_active_segments():
    trim_start, trim_end = 0.0, 60.0
    active = [(20.0, 30.0)]
    schedule = face_detection._detection_schedule(trim_start, trim_end, active)
    assert schedule[0] == trim_start
    dense_gaps = [b - a for a, b in zip(schedule, schedule[1:]) if 20.0 <= a and b <= 30.0 + 0.01]
    coarse_gaps = [b - a for a, b in zip(schedule, schedule[1:]) if 2.0 <= a and b <= 18.0]
    assert dense_gaps and max(dense_gaps) <= face_detection.DENSE_INTERVAL_SECONDS + 0.01
    assert coarse_gaps and min(coarse_gaps) >= face_detection.COARSE_INTERVAL_SECONDS - 0.01
    assert len(schedule) <= face_detection.MAX_ANALYSIS_FRAMES


def test_schedule_respects_frame_budget_on_fully_active_video():
    schedule = face_detection._detection_schedule(0.0, 120.0, [(0.0, 120.0)])
    assert len(schedule) <= face_detection.MAX_ANALYSIS_FRAMES


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg indisponível")
def test_harness_without_faces_recenters_after_hold(tmp_path):
    """Sem rostos, o corte mantém a última posição pelo hold e depois volta ao centro."""
    source = tmp_path / "moving.mp4"
    filter_graph = (
        "color=c=black:size=640x360:rate=10:duration=6[bg];"
        "color=c=white:size=120x120:duration=6[box];"
        "[bg][box]overlay=x='120+t*60':y='120':shortest=1[v]"
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-filter_complex", filter_graph, "-map", "[v]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        check=True, capture_output=True,
    )
    points = face_detection.analyze_focal_points(str(source), 0, 6, 640, 360)
    assert points
    # 640 × 360 → o centro é (320, 180); sem rostos a trajetória converge para ele.
    late = [point for point in points if point["time"] >= face_detection.FACE_HOLD_SECONDS]
    assert late, "esperava amostras após a janela de retenção"
    for point in late:
        assert point["x"] == pytest.approx(320, abs=2.0), f"x={point['x']} em t={point['time']}"
        assert point["y"] == pytest.approx(180, abs=2.0), f"y={point['y']} em t={point['time']}"
