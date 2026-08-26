import shutil
import subprocess

import numpy as np
import pytest

import face_detection


def test_crop_dimensions_preserve_vertical_aspect():
    assert face_detection.crop_dimensions(1920, 1080) == (606, 1080)
    assert face_detection.crop_dimensions(720, 1280) == (720, 1280)


def test_primary_focus_keeps_nearby_faces_together():
    focus = face_detection.select_primary_focus(
        [(700, 250, 180, 220, .9), (900, 260, 180, 220, .85)],
        None,
        1920,
        1080,
        606,
        1080,
    )
    assert focus == pytest.approx((890, 494.6))


def test_primary_focus_prefers_continuity_between_distant_faces():
    focus = face_detection.select_primary_focus(
        [(200, 200, 250, 300, .9), (1300, 200, 270, 310, .9)],
        (1450, 350),
        1920,
        1080,
        606,
        1080,
    )
    assert focus and focus[0] > 1300


def test_smoothing_ignores_head_movements_and_reacts_to_large_changes():
    ignored = face_detection.smooth_focus((500, 500), (550, 500), 606)
    moved = face_detection.smooth_focus((500, 500), (600, 500), 606)
    cut = face_detection.smooth_focus((200, 500), (1200, 500), 606)
    assert ignored == (500, 500)
    assert moved[0] == pytest.approx(534)
    assert cut[0] == pytest.approx(820)


def test_locked_face_does_not_switch_to_larger_distant_face():
    faces = [(220, 200, 120, 140, .8), (1300, 100, 400, 500, .99)]
    assert face_detection.nearest_locked_face(faces, (280, 270), 606) == faces[0]


def test_body_box_and_tracked_focus_keep_head_above_center():
    box = face_detection.body_box((100, 60, 40, 50, .9), 320, 180)
    assert box == pytest.approx((70, 37.5, 100, 142.5))
    focus = face_detection.tracked_focus(box, 320, 180, 320, 180, 180)
    assert focus[0] == pytest.approx(120)
    assert focus[1] < 180


def test_scene_cut_uses_histogram_change():
    dark = np.zeros((180, 320, 3), dtype=np.uint8)
    bright = np.full((180, 320, 3), 255, dtype=np.uint8)
    dark_histogram = face_detection.frame_histogram(dark)
    assert face_detection.is_scene_cut(dark_histogram, dark_histogram) is False
    assert face_detection.is_scene_cut(dark_histogram, face_detection.frame_histogram(bright)) is True


def test_tracker_initialization_near_frame_edges_never_breaks_analysis():
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    tracker, box = face_detection.initialize_body_tracker(frame, (0, 0, 300, 170, .9), 320, 180)
    assert (tracker is None and box is None) or box[2] <= 320 * .72 and box[3] <= 180 * .78


def test_analysis_uses_body_tracker_when_face_disappears(monkeypatch, tmp_path):
    frame = np.zeros((360, 640, 3), dtype=np.uint8)

    class Capture:
        def __init__(self):
            self.reads = 0

        def isOpened(self):
            return True

        def set(self, *_args):
            return True

        def read(self):
            self.reads += 1
            return (True, frame.copy()) if self.reads <= 3 else (False, None)

        def release(self):
            return None

    class Tracker:
        def __init__(self):
            self.updates = 0

        def update(self, _frame):
            self.updates += 1
            return True, (190, 40, 100, 180)

    tracker = Tracker()
    detections = iter([[(200, 80, 80, 100, .9)], [], []])
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")
    monkeypatch.setattr(face_detection.cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(face_detection.cv2.FaceDetectorYN, "create", lambda *_args: object())
    monkeypatch.setattr(face_detection, "detect_faces", lambda *_args: next(detections))
    monkeypatch.setattr(face_detection, "initialize_body_tracker", lambda *_args: (tracker, ()))
    points = face_detection.analyze_focal_points("video.mp4", 0, 1.5, 640, 360, model)
    assert tracker.updates == 2
    assert points[-1]["x"] != 320


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg indisponível")
def test_analysis_without_faces_returns_center_keyframes(tmp_path):
    video = tmp_path / "black.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:size=320x180:rate=10",
        "-t", "1", "-pix_fmt", "yuv420p", str(video),
    ], check=True, capture_output=True)
    points = face_detection.analyze_focal_points(str(video), 0, 1, 320, 180)
    assert points
    assert all(point["x"] == 160 and point["y"] == 90 for point in points)
