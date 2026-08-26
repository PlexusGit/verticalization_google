"""Detecção facial local e econômica para enquadramento vertical automático."""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Sequence

import cv2

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
MAX_ANALYSIS_FRAMES = 240
MIN_SAMPLE_INTERVAL_SECONDS = 0.5
MAX_ANALYSIS_WIDTH = 640
FACE_HOLD_SECONDS = 10.0
SWITCH_CONFIRM_SECONDS = 1.5
MIN_FOCAL_CHANGE_RATIO = 0.12
FOCUS_AVERAGE_SECONDS = 5.0
TRACK_MATCH_RATIO = 0.70
SCENE_CUT_CORRELATION = 0.25

Face = tuple[float, float, float, float, float]
FocalPoint = dict[str, float]
Box = tuple[float, float, float, float]


def crop_dimensions(width: int, height: int) -> tuple[int, int]:
    target_aspect = 9 / 16
    if width / height >= target_aspect:
        crop_height = height - height % 2
        crop_width = int(crop_height * target_aspect) // 2 * 2
    else:
        crop_width = width - width % 2
        crop_height = int(crop_width / target_aspect) // 2 * 2
    return crop_width, crop_height


def face_focus(face: Face, crop_height: int, frame_height: int) -> tuple[float, float]:
    x, y, width, height, _ = face
    return x + width / 2, min(frame_height, y + height / 2 + crop_height * 0.12)


def select_primary_face(
    faces: Sequence[Face],
    previous: tuple[float, float] | None,
    frame_width: int,
    frame_height: int,
    crop_width: int,
) -> Face | None:
    if not faces:
        return None
    largest_area = max(face[2] * face[3] for face in faces)
    diagonal = math.hypot(frame_width, frame_height)

    def score(face: Face) -> float:
        x, y, width, height, confidence = face
        center = (x + width / 2, y + height / 2)
        area_score = width * height / max(largest_area, 1)
        center_score = 1 - math.hypot(center[0] - frame_width / 2, center[1] - frame_height / 2) / diagonal
        continuity = 0.0
        if previous:
            continuity = max(0.0, 1 - math.hypot(center[0] - previous[0], center[1] - previous[1]) / max(crop_width, 1))
        return area_score * 0.48 + confidence * 0.17 + center_score * 0.10 + continuity * 0.25

    return max(faces, key=score)


def select_primary_focus(
    faces: Sequence[Face],
    previous: tuple[float, float] | None,
    frame_width: int,
    frame_height: int,
    crop_width: int,
    crop_height: int,
) -> tuple[float, float] | None:
    if not faces:
        return None
    left = min(face[0] for face in faces)
    top = min(face[1] for face in faces)
    right = max(face[0] + face[2] for face in faces)
    bottom = max(face[1] + face[3] for face in faces)
    if len(faces) > 1 and right - left <= crop_width * 0.82 and bottom - top <= crop_height * 0.72:
        return (left + right) / 2, min(frame_height, (top + bottom) / 2 + crop_height * 0.12)
    selected = select_primary_face(faces, previous, frame_width, frame_height, crop_width)
    return face_focus(selected, crop_height, frame_height) if selected else None


def nearest_locked_face(faces: Sequence[Face], focus: tuple[float, float] | None, crop_width: int) -> Face | None:
    if not faces or focus is None:
        return None
    nearest = min(faces, key=lambda face: math.hypot(face[0] + face[2] / 2 - focus[0], face[1] + face[3] / 2 - focus[1]))
    distance = math.hypot(nearest[0] + nearest[2] / 2 - focus[0], nearest[1] + nearest[3] / 2 - focus[1])
    return nearest if distance <= crop_width * TRACK_MATCH_RATIO else None


def smooth_focus(
    previous: tuple[float, float] | None,
    target: tuple[float, float],
    crop_width: int,
    force: bool = False,
) -> tuple[float, float]:
    if previous is None:
        return target
    distance = math.hypot(target[0] - previous[0], target[1] - previous[1])
    if not force and distance < max(8, crop_width * MIN_FOCAL_CHANGE_RATIO):
        return previous
    alpha = 0.62 if force or distance > crop_width * 0.9 else 0.34
    return previous[0] + (target[0] - previous[0]) * alpha, previous[1] + (target[1] - previous[1]) * alpha


def average_focus(
    samples: list[tuple[float, float, float]],
    timestamp: float,
    target: tuple[float, float],
    window_seconds: float = FOCUS_AVERAGE_SECONDS,
) -> tuple[float, float]:
    samples.append((timestamp, target[0], target[1]))
    cutoff = timestamp - window_seconds
    samples[:] = [sample for sample in samples if sample[0] >= cutoff]
    return (
        sum(sample[1] for sample in samples) / len(samples),
        sum(sample[2] for sample in samples) / len(samples),
    )


def analysis_frame(frame: object) -> object:
    height, width = frame.shape[:2]
    scale = min(1.0, MAX_ANALYSIS_WIDTH / width)
    return cv2.resize(frame, (round(width * scale), round(height * scale))) if scale < 1 else frame


def detect_faces(detector: object, frame: object, source_width: int, source_height: int) -> list[Face]:
    analyzed = analysis_frame(frame)
    analyzed_height, analyzed_width = analyzed.shape[:2]
    detector.setInputSize((analyzed_width, analyzed_height))
    _, detected = detector.detect(analyzed)
    if detected is None:
        return []
    scale_x, scale_y = source_width / analyzed_width, source_height / analyzed_height
    faces = []
    for row in detected:
        x, y, width, height, confidence = (float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[14]))
        if width < 18 or height < 18:
            continue
        faces.append((x * scale_x, y * scale_y, width * scale_x, height * scale_y, confidence))
    return faces


def body_box(face: Face, frame_width: int, frame_height: int) -> Box:
    x, y, width, height, _ = face
    left = max(0.0, x - width * 0.75)
    top = max(0.0, y - height * 0.45)
    right = min(float(frame_width), x + width * 1.75)
    bottom = min(float(frame_height), y + height * 3.55)
    return left, top, max(2.0, right - left), max(2.0, bottom - top)


def initialize_body_tracker(frame: object, face: Face, source_width: int, source_height: int) -> tuple[object | None, Box | None]:
    analyzed = analysis_frame(frame)
    analyzed_height, analyzed_width = analyzed.shape[:2]
    source_box = body_box(face, source_width, source_height)
    scale_x, scale_y = analyzed_width / source_width, analyzed_height / source_height
    center_x = (source_box[0] + source_box[2] / 2) * scale_x
    center_y = (source_box[1] + source_box[3] / 2) * scale_y
    width = min(source_box[2] * scale_x, analyzed_width * 0.72)
    height = min(source_box[3] * scale_y, analyzed_height * 0.78)
    left = max(2.0, min(analyzed_width - width - 2, center_x - width / 2))
    top = max(2.0, min(analyzed_height - height - 2, center_y - height / 2))
    tracker_box = (round(left), round(top), max(2, round(width)), max(2, round(height)))
    try:
        tracker = cv2.TrackerMIL_create()
        tracker.init(analyzed, tracker_box)
        return tracker, tuple(float(value) for value in tracker_box)
    except cv2.error:
        logger.debug("Não foi possível iniciar o tracker corporal", exc_info=True)
        return None, None


def tracked_focus(box: Box, analyzed_width: int, analyzed_height: int, source_width: int, source_height: int, crop_height: int) -> tuple[float, float]:
    x, y, width, height = box
    source_x = (x + width * 0.5) * source_width / analyzed_width
    source_y = (y + height * 0.25) * source_height / analyzed_height
    return source_x, min(source_height, source_y + crop_height * 0.12)


def frame_histogram(frame: object) -> object:
    analyzed = analysis_frame(frame)
    gray = cv2.cvtColor(analyzed, cv2.COLOR_BGR2GRAY)
    histogram = cv2.calcHist([gray], [0], None, [32], [0, 256])
    return cv2.normalize(histogram, histogram).flatten()


def is_scene_cut(previous_histogram: object | None, current_histogram: object) -> bool:
    if previous_histogram is None:
        return False
    return cv2.compareHist(previous_histogram, current_histogram, cv2.HISTCMP_CORREL) < SCENE_CUT_CORRELATION


def analyze_focal_points(
    video_path: str,
    trim_start: float,
    trim_end: float,
    source_width: int,
    source_height: int,
    model_path: Path = MODEL_PATH,
) -> tuple[FocalPoint, ...]:
    if not model_path.is_file():
        raise RuntimeError(f"modelo de detecção facial ausente: {model_path}")
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise RuntimeError("não foi possível abrir o vídeo para detectar rostos")

    duration = trim_end - trim_start
    interval = max(MIN_SAMPLE_INTERVAL_SECONDS, duration / MAX_ANALYSIS_FRAMES)
    crop_width, crop_height = crop_dimensions(source_width, source_height)
    detector = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), 0.65, 0.3, 20)
    center = (source_width / 2, source_height / 2)
    current_focus: tuple[float, float] | None = None
    emitted_focus: tuple[float, float] | None = None
    pending_focus: tuple[float, float] | None = None
    pending_since: float | None = None
    last_confirmed_time: float | None = None
    tracker: object | None = None
    tracker_seed_frame: object | None = None
    tracker_seed_face: Face | None = None
    previous_histogram: object | None = None
    focus_samples: list[tuple[float, float, float]] = []
    keyframes: list[FocalPoint] = []
    timestamp = trim_start
    analyzed_frames = 0
    tracked_frames = 0
    scene_cuts = 0

    try:
        while timestamp < trim_end and analyzed_frames < MAX_ANALYSIS_FRAMES:
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                break
            analyzed = analysis_frame(frame)
            analyzed_height, analyzed_width = analyzed.shape[:2]
            histogram = frame_histogram(frame)
            scene_cut = is_scene_cut(previous_histogram, histogram)
            previous_histogram = histogram
            if scene_cut:
                current_focus = None
                pending_focus = None
                pending_since = None
                last_confirmed_time = None
                tracker = None
                tracker_seed_frame = None
                tracker_seed_face = None
                focus_samples.clear()
                scene_cuts += 1

            faces = detect_faces(detector, frame, source_width, source_height)
            locked_face = nearest_locked_face(faces, current_focus, crop_width)
            selected_face: Face | None = None
            target: tuple[float, float] | None = None
            observed_target = False

            if current_focus is None and faces:
                selected_face = select_primary_face(faces, None, source_width, source_height, crop_width)
            elif locked_face is not None:
                selected_face = locked_face

            if selected_face is not None:
                target = face_focus(selected_face, crop_height, source_height)
                observed_target = True
                last_confirmed_time = timestamp
                pending_focus = None
                pending_since = None
                tracker = None
                tracker_seed_frame = frame.copy()
                tracker_seed_face = selected_face
            elif not scene_cut:
                if tracker is None and tracker_seed_frame is not None and tracker_seed_face is not None:
                    tracker, _ = initialize_body_tracker(tracker_seed_frame, tracker_seed_face, source_width, source_height)
                    tracker_seed_frame = None
                    tracker_seed_face = None
                if tracker is not None:
                    tracking_ok, tracker_box = tracker.update(analyzed)
                    if tracking_ok:
                        target = tracked_focus(tuple(float(value) for value in tracker_box), analyzed_width, analyzed_height, source_width, source_height, crop_height)
                        observed_target = True
                        tracked_frames += 1
                    else:
                        tracker = None

            if target is None and faces:
                candidate_face = select_primary_face(faces, current_focus, source_width, source_height, crop_width)
                candidate_focus = face_focus(candidate_face, crop_height, source_height) if candidate_face else None
                if candidate_focus is not None:
                    if pending_focus is None or math.hypot(candidate_focus[0] - pending_focus[0], candidate_focus[1] - pending_focus[1]) > crop_width * TRACK_MATCH_RATIO:
                        pending_focus = candidate_focus
                        pending_since = timestamp
                    else:
                        pending_focus = candidate_focus
                    if current_focus is None or pending_since is not None and timestamp - pending_since >= SWITCH_CONFIRM_SECONDS:
                        target = candidate_focus
                        observed_target = True
                        focus_samples.clear()
                        last_confirmed_time = timestamp
                        pending_focus = None
                        pending_since = None
                        if candidate_face:
                            tracker = None
                            tracker_seed_frame = frame.copy()
                            tracker_seed_face = candidate_face

            if observed_target and target is not None:
                target = average_focus(focus_samples, timestamp, target)

            if target is None:
                if current_focus is not None and last_confirmed_time is not None and timestamp - last_confirmed_time <= FACE_HOLD_SECONDS:
                    target = current_focus
                else:
                    target = center
                    focus_samples.clear()

            stabilized = smooth_focus(current_focus, target, crop_width, force=scene_cut)
            minimum_change = max(8, crop_width * MIN_FOCAL_CHANGE_RATIO)
            movement = math.inf if emitted_focus is None else math.hypot(stabilized[0] - emitted_focus[0], stabilized[1] - emitted_focus[1])
            if emitted_focus is None or scene_cut or movement >= minimum_change:
                keyframes.append({"time": round(timestamp, 3), "x": round(stabilized[0], 2), "y": round(stabilized[1], 2)})
                emitted_focus = stabilized
            current_focus = stabilized
            analyzed_frames += 1
            timestamp += interval
    finally:
        capture.release()

    if current_focus is not None and (not keyframes or keyframes[-1]["time"] < trim_end):
        keyframes.append({"time": round(trim_end, 3), "x": round(current_focus[0], 2), "y": round(current_focus[1], 2)})
    logger.info(
        "Análise estável concluída: %d frames, %d keyframes, %d frames rastreados, %d cortes de cena, intervalo %.2fs",
        analyzed_frames,
        len(keyframes),
        tracked_frames,
        scene_cuts,
        interval,
    )
    return tuple(keyframes)
