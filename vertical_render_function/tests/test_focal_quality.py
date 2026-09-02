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


def test_content_saliency_peaks_on_the_bright_blob():
    gray = np.zeros((180, 320), dtype=np.uint8)
    gray[60:120, 200:280] = 255
    saliency, contrast = face_detection.content_saliency(None, gray)
    assert saliency is not None and contrast > 0
    focus = face_detection.saliency_focus(saliency, 320, 180, 1920, 1080)
    assert focus is not None
    # Blob center is x=240/320 (75%) and y=90/180 (50%).
    assert focus[0] == pytest.approx(0.75 * 1920, abs=1920 * 0.15)
    assert focus[1] == pytest.approx(0.50 * 1080, abs=1080 * 0.20)


def test_content_saliency_uses_motion_when_available():
    previous = np.zeros((180, 320), dtype=np.uint8)
    current = np.zeros((180, 320), dtype=np.uint8)
    current[40:100, 40:120] = 255  # appeared -> motion exactly there
    saliency, contrast = face_detection.content_saliency(previous, current)
    assert saliency is not None and contrast >= face_detection.SALIENCY_PEAK_THRESHOLD
    focus = face_detection.saliency_focus(saliency, 320, 180, 1920, 1080)
    assert focus is not None
    assert focus[0] == pytest.approx(0.25 * 1920, abs=1920 * 0.15)
    assert focus[1] == pytest.approx(0.39 * 1080, abs=1080 * 0.20)


def test_content_saliency_rejects_flat_frames():
    gray = np.full((180, 320), 128, dtype=np.uint8)
    saliency, contrast = face_detection.content_saliency(None, gray)
    assert contrast < face_detection.SALIENCY_PEAK_THRESHOLD


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
def test_harness_trajectory_follows_known_moving_subject(tmp_path):
    """A white box sliding left-to-right must be followed by the focal path."""
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
    assert len(points) >= 4

    crop_width, _ = face_detection.crop_dimensions(640, 360)

    def box_center_x(time_seconds: float) -> float:
        return 120 + 60 * time_seconds + 60  # overlay x + half box width

    inside = 0
    total = 0
    jitter_samples = []
    previous_x = None
    for point in points:
        truth = box_center_x(point["time"])
        total += 1
        if abs(point["x"] - truth) <= crop_width / 2:
            inside += 1
        if previous_x is not None:
            jitter_samples.append(abs(point["x"] - previous_x))
        previous_x = point["x"]

    in_crop_ratio = inside / total
    max_step = max(jitter_samples) if jitter_samples else 0.0
    # The subject must stay framed most of the time and the path must not teleport.
    assert in_crop_ratio >= 0.8, f"subject in crop only {in_crop_ratio:.0%} of the time"
    assert max_step <= crop_width * 0.5, f"jitter spike of {max_step:.0f}px between keyframes"
