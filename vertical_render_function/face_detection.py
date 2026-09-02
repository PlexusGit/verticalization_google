"""Detecção facial local e econômica para enquadramento vertical automático.

Estratégia (versão 2):
- Duas passadas: uma varredura barata localiza segmentos ativos e cortes de cena;
  a detecção facial (cara) é gastа apenas onde há movimento.
- YuNet detecta rostos; o fluxo óptico (Lucas-Kanade com verificação direta e
  reversa) mantém o enquadramento entre amostras, com CSRT como reserva.
- O foco prediz a trajetória (lead por velocidade) e a suavização se adapta à
  velocidade e à confiança da detecção.
- Sem rostos, o corte mantém a última posição por alguns segundos e depois
  volta ao centro do vídeo.
- Entre várias faces, a atividade dos lábios escolhe quem fala, com histerese.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
FOCAL_STRATEGY_VERSION = 3
MAX_ANALYSIS_FRAMES = 240
MIN_SAMPLE_INTERVAL_SECONDS = 0.5
MAX_ANALYSIS_WIDTH = 640
FACE_HOLD_SECONDS = 3.0
SWITCH_CONFIRM_SECONDS = 0.6
SPEAKER_SWITCH_RATIO = 1.2
MIN_FOCAL_CHANGE_RATIO = 0.15
FOCUS_AVERAGE_SECONDS = 5.0
FAST_AVERAGE_SECONDS = 1.0
TRACK_MATCH_RATIO = 0.70
SCENE_CUT_CORRELATION = 0.25

# Adaptive tracking: the crop anticipates fast subjects and holds steady when
# confidence drops, instead of using one fixed smoothing profile.
FAST_SPEED_RATIO = 0.25
FAST_MIN_CHANGE_RATIO = 0.06
LEAD_SECONDS = 0.30
MAX_LEAD_RATIO = 0.25
LOW_CONFIDENCE = 0.75

# Composition: keep the face slightly above center with margin, and lean the
# crop toward where the person is looking.
HEADROOM_RATIO = 0.40
FACE_MARGIN_RATIO = 0.15
GAZE_BIAS_RATIO = 0.08

# Scene analysis: hard cuts and fades reset tracking but ease the crop across
# the boundary instead of snapping to the frame center.
EDGE_CUT_RATIO = 0.38
FADE_CORRELATION_HIGH = 0.60
FADE_CONFIRM_SAMPLES = 3
SCENE_BLEND_SECONDS = 0.8

# Two-pass sampling: cheap scan finds active spans; face detection concentrates
# where things happen.
SCAN_INTERVAL_SECONDS = 0.75
SCAN_MAX_SAMPLES = 300
DENSE_INTERVAL_SECONDS = 0.18
COARSE_INTERVAL_SECONDS = 1.1
MOTION_ACTIVE_RATIO = 0.015
ACTIVE_PADDING_SECONDS = 1.0
ACTIVE_MERGE_GAP_SECONDS = 1.0
FLOW_MAX_GAP_SECONDS = 0.6
VELOCITY_WINDOW_SECONDS = 1.2

Face = tuple[float, float, float, float, float]
Landmarks = tuple[tuple[float, float], ...]
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


def clamp_focus_to_face(
    focus: tuple[float, float],
    face: Sequence[float],
    crop_width: int,
    crop_height: int,
    frame_width: int,
    frame_height: int,
) -> tuple[float, float]:
    """Keep the face (plus margin) inside the crop window whenever possible."""
    fx, fy = focus
    x, y, width, height = face[0], face[1], face[2], face[3]
    margin_x, margin_y = width * FACE_MARGIN_RATIO, height * FACE_MARGIN_RATIO
    low_x, high_x = x + width + margin_x - crop_width / 2, x - margin_x + crop_width / 2
    if low_x <= high_x:
        fx = min(max(fx, low_x), high_x)
    else:
        fx = x + width / 2
    low_y, high_y = y + height + margin_y - crop_height / 2, y - margin_y + crop_height / 2
    if low_y <= high_y:
        fy = min(max(fy, low_y), high_y)
    else:
        fy = y + height / 2
    if frame_width > crop_width:
        fx = min(max(fx, crop_width / 2), frame_width - crop_width / 2)
    else:
        fx = frame_width / 2
    if frame_height > crop_height:
        fy = min(max(fy, crop_height / 2), frame_height - crop_height / 2)
    else:
        fy = frame_height / 2
    return fx, fy


def face_focus(
    face: Face,
    crop_height: int,
    frame_height: int,
    crop_width: int | None = None,
    frame_width: int | None = None,
    landmarks: Landmarks | None = None,
) -> tuple[float, float]:
    """Focal point that leaves headroom and leans toward the gaze direction."""
    x, y, width, height, _ = face
    center_x = x + width / 2
    center_y = y + height / 2 + crop_height * (0.5 - HEADROOM_RATIO)
    if landmarks and len(landmarks) >= 5 and width > 0:
        eye_mid_x = (landmarks[0][0] + landmarks[1][0]) / 2
        gaze_offset = (landmarks[2][0] - eye_mid_x) / width
        bias = max(-GAZE_BIAS_RATIO, min(GAZE_BIAS_RATIO, gaze_offset * 2.0))
        if crop_width:
            center_x += bias * crop_width
    if crop_width and frame_width:
        return clamp_focus_to_face((center_x, center_y), face, crop_width, crop_height, frame_width, frame_height)
    return center_x, min(frame_height, center_y)


def select_primary_face(
    faces: Sequence[Face],
    previous: tuple[float, float] | None,
    frame_width: int,
    frame_height: int,
    crop_width: int,
    lip_activity: Sequence[float] | None = None,
) -> Face | None:
    if not faces:
        return None
    largest_area = max(face[2] * face[3] for face in faces)
    diagonal = math.hypot(frame_width, frame_height)
    activities = [max(0.0, value) for value in lip_activity] if lip_activity else [0.0] * len(faces)
    peak_activity = max(activities) if activities else 0.0
    use_speech = bool(lip_activity) and peak_activity > 0

    def score(index: int) -> float:
        face = faces[index]
        x, y, width, height, confidence = face
        center = (x + width / 2, y + height / 2)
        area_score = width * height / max(largest_area, 1)
        center_score = 1 - math.hypot(center[0] - frame_width / 2, center[1] - frame_height / 2) / diagonal
        continuity = 0.0
        if previous:
            continuity = max(0.0, 1 - math.hypot(center[0] - previous[0], center[1] - previous[1]) / max(crop_width, 1))
        if use_speech:
            lip = activities[index] / peak_activity
            return area_score * 0.28 + confidence * 0.12 + center_score * 0.08 + continuity * 0.22 + lip * 0.30
        return area_score * 0.48 + confidence * 0.17 + center_score * 0.10 + continuity * 0.25

    return faces[max(range(len(faces)), key=score)]


def select_primary_focus(
    faces: Sequence[Face],
    previous: tuple[float, float] | None,
    frame_width: int,
    frame_height: int,
    crop_width: int,
    crop_height: int,
    landmarks: Sequence[Landmarks] | None = None,
    lip_activity: Sequence[float] | None = None,
) -> tuple[float, float] | None:
    if not faces:
        return None
    left = min(face[0] for face in faces)
    top = min(face[1] for face in faces)
    right = max(face[0] + face[2] for face in faces)
    bottom = max(face[1] + face[3] for face in faces)
    if len(faces) > 1 and right - left <= crop_width * 0.82 and bottom - top <= crop_height * 0.72:
        group = (left, top, right - left, bottom - top)
        focus = ((left + right) / 2, (top + bottom) / 2 + crop_height * (0.5 - HEADROOM_RATIO))
        return clamp_focus_to_face(focus, group, crop_width, crop_height, frame_width, frame_height)
    selected_index = None
    selected = select_primary_face(faces, previous, frame_width, frame_height, crop_width, lip_activity)
    if selected is None:
        return None
    for index, face in enumerate(faces):
        if face is selected or tuple(face) == tuple(selected):
            selected_index = index
            break
    face_landmarks = landmarks[selected_index] if landmarks and selected_index is not None and selected_index < len(landmarks) else None
    return face_focus(selected, crop_height, frame_height, crop_width, frame_width, face_landmarks)


def speaker_scores(
    faces: Sequence[Face],
    anchor: tuple[float, float] | None,
    activities: Sequence[float],
    crop_width: int,
) -> list[float]:
    """Relative prominence of each face as the likely speaker."""
    largest_area = max((face[2] * face[3] for face in faces), default=1.0)
    peak = max((value for value in activities), default=0.0) or 1.0
    scores = []
    for face, activity in zip(faces, activities):
        center = (face[0] + face[2] / 2, face[1] + face[3] / 2)
        area_score = face[2] * face[3] / max(largest_area, 1)
        continuity = 0.0
        if anchor:
            continuity = max(0.0, 1 - math.hypot(center[0] - anchor[0], center[1] - anchor[1]) / max(crop_width, 1))
        scores.append((activity / peak) * 0.60 + area_score * 0.25 + continuity * 0.15)
    return scores


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
    speed: float | None = None,
    confidence: float | None = None,
) -> tuple[float, float]:
    """Adaptive smoothing: tight when calm, responsive when the subject runs."""
    if previous is None:
        return target
    distance = math.hypot(target[0] - previous[0], target[1] - previous[1])
    fast = speed is not None and speed > crop_width * FAST_SPEED_RATIO
    minimum_change = max(8, crop_width * (FAST_MIN_CHANGE_RATIO if fast and not force else MIN_FOCAL_CHANGE_RATIO))
    if not force and distance < minimum_change:
        return previous
    if force or distance > crop_width * 0.9:
        alpha = 0.62
    elif fast:
        alpha = 0.55
    else:
        alpha = 0.34
    if confidence is not None and confidence < LOW_CONFIDENCE:
        alpha *= 0.6
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


def detect_faces(
    detector: object,
    frame: object,
    source_width: int,
    source_height: int,
    landmarks_out: list[Landmarks] | None = None,
) -> list[Face]:
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
        if landmarks_out is not None:
            points = tuple((float(row[4 + index * 2]) * scale_x, float(row[5 + index * 2]) * scale_y) for index in range(5))
            landmarks_out.append(points)
    return faces


def body_box(face: Face, frame_width: int, frame_height: int) -> Box:
    x, y, width, height, _ = face
    left = max(0.0, x - width * 0.75)
    top = max(0.0, y - height * 0.45)
    right = min(float(frame_width), x + width * 1.75)
    bottom = min(float(frame_height), y + height * 3.55)
    return left, top, max(2.0, right - left), max(2.0, bottom - top)


def _create_body_tracker():
    """CSRT when the contrib build provides it; MIL keeps the fallback working."""
    for name in ("TrackerCSRT_create", "TrackerMIL_create"):
        factory = getattr(cv2, name, None)
        if factory is None:
            continue
        try:
            return factory()
        except cv2.error:
            continue
    return None


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
    tracker = _create_body_tracker()
    if tracker is None:
        return None, None
    try:
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


def flow_track(previous_gray: object, current_gray: object, points: Sequence[Sequence[float]]) -> tuple[float, float] | None:
    """Median Lucas-Kanade displacement with a forward-backward sanity check."""
    if previous_gray is None or current_gray is None or points is None or len(points) < 4:
        return None
    initial = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01)
    try:
        forward, status_f, _ = cv2.calcOpticalFlowPyrLK(previous_gray, current_gray, initial, None, winSize=(21, 21), maxLevel=3, criteria=criteria)
        backward, status_b, _ = cv2.calcOpticalFlowPyrLK(current_gray, previous_gray, forward, None, winSize=(21, 21), maxLevel=3, criteria=criteria)
    except cv2.error:
        return None
    fb_error = np.linalg.norm(backward.reshape(-1, 2) - initial.reshape(-1, 2), axis=1)
    good = (status_f.reshape(-1) == 1) & (status_b.reshape(-1) == 1) & (fb_error < 2.5)
    if int(good.sum()) < max(3, int(len(points) * 0.5)):
        return None
    displacement = forward.reshape(-1, 2)[good] - initial.reshape(-1, 2)[good]
    dx, dy = np.median(displacement, axis=0)
    return float(dx), float(dy)


def mouth_region(
    face: Face,
    landmarks: Landmarks | None,
    scale_x: float,
    scale_y: float,
    analyzed_width: int,
    analyzed_height: int,
) -> Box | None:
    x, y, width, height, _ = face
    if landmarks and len(landmarks) >= 5:
        mouth_x = (landmarks[3][0] + landmarks[4][0]) / 2
        mouth_y = (landmarks[3][1] + landmarks[4][1]) / 2
    else:
        mouth_x, mouth_y = x + width / 2, y + height * 0.72
    box_width = max(6.0, width * 0.55) * scale_x
    box_height = max(6.0, height * 0.35) * scale_y
    left = int(max(0, mouth_x * scale_x - box_width / 2))
    top = int(max(0, mouth_y * scale_y - box_height / 2))
    right = int(min(analyzed_width, left + box_width))
    bottom = int(min(analyzed_height, top + box_height))
    if right - left < 3 or bottom - top < 3:
        return None
    return left, top, right, bottom


def lip_energy(previous_gray: object, current_gray: object, box: Box | None) -> float:
    """Mean absolute change inside the mouth region, normalized to 0-1."""
    if box is None or previous_gray is None or current_gray is None:
        return 0.0
    left, top, right, bottom = box
    previous_crop = previous_gray[top:bottom, left:right].astype(np.float32)
    current_crop = current_gray[top:bottom, left:right].astype(np.float32)
    if previous_crop.shape != current_crop.shape or previous_crop.size == 0:
        return 0.0
    return float(np.mean(np.abs(current_crop - previous_crop)) / 255.0)


def frame_histogram(frame: object) -> object:
    analyzed = analysis_frame(frame)
    gray = cv2.cvtColor(analyzed, cv2.COLOR_BGR2GRAY)
    histogram = cv2.calcHist([gray], [0], None, [32], [0, 256])
    return cv2.normalize(histogram, histogram).flatten()


def is_scene_cut(previous_histogram: object | None, current_histogram: object) -> bool:
    if previous_histogram is None:
        return False
    return cv2.compareHist(previous_histogram, current_histogram, cv2.HISTCMP_CORREL) < SCENE_CUT_CORRELATION


def edge_change_ratio(previous_small: object | None, current_small: object, threshold: int = 25) -> float:
    """Fraction of clearly changed pixels between two small grayscale frames."""
    if previous_small is None or previous_small.shape != current_small.shape:
        return 0.0
    diff = cv2.absdiff(current_small, previous_small)
    return float(np.count_nonzero(diff > threshold)) / float(diff.size)


def _scan_activity(capture: object, trim_start: float, trim_end: float) -> list[tuple[float, float]]:
    """Cheap first pass: per-sample (time, motion) used to find active spans."""
    duration = trim_end - trim_start
    interval = max(0.5, min(SCAN_INTERVAL_SECONDS, duration / SCAN_MAX_SAMPLES))
    samples: list[tuple[float, float]] = []
    previous_tiny: object | None = None
    timestamp = trim_start
    while timestamp < trim_end and len(samples) < SCAN_MAX_SAMPLES:
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ok, frame = capture.read()
        if not ok:
            break
        gray = cv2.cvtColor(analysis_frame(frame), cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        tiny = cv2.resize(gray, (160, max(1, round(height * 160 / width))))
        motion = float(np.mean(cv2.absdiff(tiny, previous_tiny))) / 255.0 if previous_tiny is not None else 0.0
        samples.append((timestamp, motion))
        previous_tiny = tiny
        timestamp += interval
    return samples


def _active_segments(samples: Sequence[tuple[float, float]], trim_start: float, trim_end: float) -> list[tuple[float, float]]:
    threshold = MOTION_ACTIVE_RATIO
    spans: list[tuple[float, float]] = []
    for timestamp, motion in samples:
        if motion <= threshold:
            continue
        if spans and timestamp - spans[-1][1] <= ACTIVE_MERGE_GAP_SECONDS:
            spans[-1] = (spans[-1][0], timestamp)
        else:
            spans.append((timestamp, timestamp))
    padded = []
    for start, end in spans:
        padded.append((max(trim_start, start - ACTIVE_PADDING_SECONDS), min(trim_end, end + ACTIVE_PADDING_SECONDS)))
    merged: list[tuple[float, float]] = []
    for start, end in padded:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _detection_schedule(trim_start: float, trim_end: float, active: Sequence[tuple[float, float]]) -> list[float]:
    """Dense samples on active spans, coarse elsewhere, within the frame budget."""
    candidates: set[float] = {round(trim_start, 3)}
    for start, end in active:
        timestamp = start
        while timestamp <= end:
            candidates.add(round(timestamp, 3))
            timestamp += DENSE_INTERVAL_SECONDS
    cursor = trim_start
    for start, end in [*active, (trim_end, trim_end)]:
        if start > cursor:
            timestamp = cursor
            while timestamp <= min(start, trim_end):
                candidates.add(round(timestamp, 3))
                timestamp += COARSE_INTERVAL_SECONDS
        cursor = max(cursor, end)
    times = sorted(candidates)
    while len(times) > MAX_ANALYSIS_FRAMES:
        times = times[::2]
    return times


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
    crop_width, crop_height = crop_dimensions(source_width, source_height)
    detector = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), 0.65, 0.3, 20)
    center = (source_width / 2, source_height / 2)
    try:
        activity = _scan_activity(capture, trim_start, trim_end)
        schedule = _detection_schedule(trim_start, trim_end, _active_segments(activity, trim_start, trim_end))
        keyframes, meta = _track_trajectory(
            capture, detector, schedule, trim_start, trim_end, source_width, source_height, crop_width, crop_height, center
        )
    finally:
        capture.release()

    logger.info(
        "Análise estável concluída: %d frames, %d keyframes, %d frames rastreados, %d cortes de cena",
        meta["analyzed"],
        len(keyframes),
        meta["tracked"],
        meta["cuts"],
    )
    return tuple(keyframes)


def _track_trajectory(
    capture: object,
    detector: object,
    schedule: Sequence[float],
    trim_start: float,
    trim_end: float,
    source_width: int,
    source_height: int,
    crop_width: int,
    crop_height: int,
    center: tuple[float, float],
) -> tuple[list[FocalPoint], dict[str, int]]:
    analyzed_scale_x = MAX_ANALYSIS_WIDTH / source_width if source_width > MAX_ANALYSIS_WIDTH else 1.0
    analyzed_scale_y = analyzed_scale_x
    current_focus: tuple[float, float] | None = None
    emitted_focus: tuple[float, float] | None = None
    last_confirmed_time: float | None = None
    speaker_center: tuple[float, float] | None = None
    challenge_since: float | None = None
    tracker: object | None = None
    tracker_seed_frame: object | None = None
    tracker_seed_face: Face | None = None
    previous_gray: object | None = None
    previous_faces: list[Face] = []
    flow_box: Box | None = None
    flow_time: float | None = None
    previous_histogram: object | None = None
    previous_tiny: object | None = None
    fade_streak = 0
    suppress_emission = False
    focus_samples: list[tuple[float, float, float]] = []
    keyframes: list[FocalPoint] = []
    meta = {"analyzed": 0, "tracked": 0, "cuts": 0}

    def emit(time_seconds: float, focus: tuple[float, float]) -> None:
        nonlocal emitted_focus
        keyframes.append({"time": round(time_seconds, 3), "x": round(focus[0], 2), "y": round(focus[1], 2)})
        emitted_focus = focus

    for timestamp in schedule:
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ok, frame = capture.read()
        if not ok:
            break
        analyzed = analysis_frame(frame)
        analyzed_height, analyzed_width = analyzed.shape[:2]
        gray = cv2.cvtColor(analyzed, cv2.COLOR_BGR2GRAY)
        tiny = cv2.resize(gray, (160, max(1, round(analyzed_height * 160 / analyzed_width))))
        histogram = frame_histogram(frame)
        correlation = cv2.compareHist(previous_histogram, histogram, cv2.HISTCMP_CORREL) if previous_histogram is not None else 1.0
        cut = is_scene_cut(previous_histogram, histogram) or edge_change_ratio(previous_tiny, tiny) > EDGE_CUT_RATIO
        fade = FADE_CORRELATION_HIGH > correlation >= SCENE_CUT_CORRELATION
        fade_streak = fade_streak + 1 if fade else 0
        scene_event = cut or fade_streak >= FADE_CONFIRM_SAMPLES
        previous_histogram = histogram
        previous_tiny = tiny

        if scene_event:
            meta["cuts"] += 1
            if current_focus is not None:
                emit(timestamp, current_focus)
                suppress_emission = True
            speaker_center = None
            challenge_since = None
            tracker = None
            tracker_seed_frame = None
            tracker_seed_face = None
            flow_box = None
            flow_time = None
            focus_samples.clear()
            fade_streak = 0

        landmarks: list[Landmarks] = []
        faces = detect_faces(detector, frame, source_width, source_height, landmarks)
        activities = [0.0] * len(faces)
        if faces and previous_gray is not None and previous_faces:
            for index, face in enumerate(faces):
                face_center = (face[0] + face[2] / 2, face[1] + face[3] / 2)
                match = min(
                    range(len(previous_faces)),
                    key=lambda i: math.hypot(
                        previous_faces[i][0] + previous_faces[i][2] / 2 - face_center[0],
                        previous_faces[i][1] + previous_faces[i][3] / 2 - face_center[1],
                    ),
                    default=None,
                )
                if match is None:
                    continue
                previous_center = (previous_faces[match][0] + previous_faces[match][2] / 2, previous_faces[match][1] + previous_faces[match][3] / 2)
                if math.hypot(previous_center[0] - face_center[0], previous_center[1] - face_center[1]) > crop_width * TRACK_MATCH_RATIO:
                    continue
                box = mouth_region(face, landmarks[index] if index < len(landmarks) else None, analyzed_scale_x, analyzed_scale_y, analyzed_width, analyzed_height)
                activities[index] = lip_energy(previous_gray, gray, box)

        selected_face: Face | None = None
        selected_landmarks: Landmarks | None = None
        confidence: float | None = None
        target: tuple[float, float] | None = None
        observed = False

        if faces:
            if speaker_center is not None:
                incumbent_index = None
                incumbent_distance = math.inf
                for index, face in enumerate(faces):
                    face_center = (face[0] + face[2] / 2, face[1] + face[3] / 2)
                    distance = math.hypot(face_center[0] - speaker_center[0], face_center[1] - speaker_center[1])
                    if distance < incumbent_distance:
                        incumbent_distance, incumbent_index = distance, index
                if incumbent_index is not None and incumbent_distance > crop_width * TRACK_MATCH_RATIO:
                    incumbent_index = None
                challenger_index = None
                if incumbent_index is not None:
                    scores = speaker_scores(faces, speaker_center, activities, crop_width)
                    incumbent_score = scores[incumbent_index]
                    for index in range(len(faces)):
                        if index != incumbent_index and scores[index] > incumbent_score * SPEAKER_SWITCH_RATIO:
                            if challenger_index is None or scores[index] > scores[challenger_index]:
                                challenger_index = index
                    if challenger_index is not None:
                        if challenge_since is None:
                            challenge_since = timestamp
                        elif timestamp - challenge_since >= SWITCH_CONFIRM_SECONDS:
                            incumbent_index, challenge_since = challenger_index, None
                    else:
                        challenge_since = None
                selected_index = incumbent_index
                if selected_index is None:
                    selected_index = max(range(len(faces)), key=lambda i: speaker_scores(faces, speaker_center, activities, crop_width)[i])
                    challenge_since = None
            else:
                selected_index = None
                best = select_primary_face(faces, current_focus, source_width, source_height, crop_width, activities)
                for index, face in enumerate(faces):
                    if face is best or tuple(face) == tuple(best or ()):
                        selected_index = index
                        break
                challenge_since = None
            if selected_index is not None:
                selected_face = faces[selected_index]
                selected_landmarks = landmarks[selected_index] if selected_index < len(landmarks) else None
                confidence = selected_face[4]
                target = face_focus(selected_face, crop_height, source_height, crop_width, source_width, selected_landmarks)
                observed = True
                last_confirmed_time = timestamp
                speaker_center = (selected_face[0] + selected_face[2] / 2, selected_face[1] + selected_face[3] / 2)
                tracker = None
                tracker_seed_frame = frame.copy()
                tracker_seed_face = selected_face
                corners = [
                    (selected_face[0], selected_face[1]),
                    (selected_face[0] + selected_face[2], selected_face[1]),
                    (selected_face[0], selected_face[1] + selected_face[3]),
                    (selected_face[0] + selected_face[2], selected_face[1] + selected_face[3]),
                ]
                if selected_landmarks:
                    corners.extend(selected_landmarks)
                flow_box = (
                    selected_face[0] * analyzed_scale_x,
                    selected_face[1] * analyzed_scale_y,
                    selected_face[2] * analyzed_scale_x,
                    selected_face[3] * analyzed_scale_y,
                )
                flow_time = timestamp

        if target is None and flow_box is not None and flow_time is not None and timestamp - flow_time <= FLOW_MAX_GAP_SECONDS and previous_gray is not None:
            corners = [
                (flow_box[0], flow_box[1]),
                (flow_box[0] + flow_box[2], flow_box[1]),
                (flow_box[0], flow_box[1] + flow_box[3]),
                (flow_box[0] + flow_box[2], flow_box[1] + flow_box[3]),
            ]
            displacement = flow_track(previous_gray, gray, corners)
            if displacement is not None:
                moved = (flow_box[0] + displacement[0], flow_box[1] + displacement[1], flow_box[2], flow_box[3])
                flow_box = moved
                flow_time = timestamp
                source_box = (
                    moved[0] / analyzed_scale_x,
                    moved[1] / analyzed_scale_y,
                    moved[2] / analyzed_scale_x,
                    moved[3] / analyzed_scale_y,
                )
                target = tracked_focus(source_box, analyzed_width, analyzed_height, source_width, source_height, crop_height)
                observed = True
                confidence = 0.7
                meta["tracked"] += 1
                tracker_seed_frame = frame.copy()
                tracker_seed_face = (source_box[0], source_box[1], source_box[2], source_box[3], 0.7)

        if target is None and not scene_event:
            if tracker is None and tracker_seed_frame is not None and tracker_seed_face is not None:
                tracker, _ = initialize_body_tracker(tracker_seed_frame, tracker_seed_face, source_width, source_height)
                tracker_seed_frame = None
                tracker_seed_face = None
            if tracker is not None:
                tracking_ok, tracker_box = tracker.update(analyzed)
                if tracking_ok:
                    target = tracked_focus(tuple(float(value) for value in tracker_box), analyzed_width, analyzed_height, source_width, source_height, crop_height)
                    observed = True
                    confidence = 0.6
                    meta["tracked"] += 1
                else:
                    tracker = None

        # Velocity comes from the observed targets, not the stabilized focus:
        # measuring the lagged crop would underestimate speed and permanently
        # keep the smoothing in its slow profile.
        velocity = (0.0, 0.0)
        if focus_samples:
            velocity_cutoff = timestamp - VELOCITY_WINDOW_SECONDS
            velocity_window = [sample for sample in focus_samples if sample[0] >= velocity_cutoff]
            if len(velocity_window) >= 2:
                span = velocity_window[-1][0] - velocity_window[0][0]
                if span > 0.05:
                    velocity = (
                        (velocity_window[-1][1] - velocity_window[0][1]) / span,
                        (velocity_window[-1][2] - velocity_window[0][2]) / span,
                    )
        speed = math.hypot(*velocity)

        if observed and target is not None:
            # A long window steadies a static camera; while the subject moves
            # it would trail behind, so shorten it in proportion to the speed.
            window = FOCUS_AVERAGE_SECONDS if speed <= crop_width * FAST_SPEED_RATIO else FAST_AVERAGE_SECONDS
            target = average_focus(focus_samples, timestamp, target, window)

        if target is None:
            if current_focus is not None and last_confirmed_time is not None and timestamp - last_confirmed_time <= FACE_HOLD_SECONDS:
                target = current_focus
            else:
                target = center
                focus_samples.clear()

        lead_x = max(-crop_width * MAX_LEAD_RATIO, min(crop_width * MAX_LEAD_RATIO, velocity[0] * LEAD_SECONDS))
        lead_y = max(-crop_height * MAX_LEAD_RATIO, min(crop_height * MAX_LEAD_RATIO, velocity[1] * LEAD_SECONDS))
        target = (target[0] + lead_x, target[1] + lead_y)

        stabilized = smooth_focus(current_focus, target, crop_width, force=scene_event, speed=speed, confidence=confidence)
        minimum_change = max(8, crop_width * (FAST_MIN_CHANGE_RATIO if speed > crop_width * FAST_SPEED_RATIO else MIN_FOCAL_CHANGE_RATIO))
        movement = math.inf if emitted_focus is None else math.hypot(stabilized[0] - emitted_focus[0], stabilized[1] - emitted_focus[1])
        if not suppress_emission and (emitted_focus is None or movement >= minimum_change):
            emit(timestamp, stabilized)
        suppress_emission = False
        current_focus = stabilized
        previous_gray = gray
        previous_faces = faces
        meta["analyzed"] += 1

    if current_focus is not None and (not keyframes or keyframes[-1]["time"] < trim_end):
        keyframes.append({"time": round(trim_end, 3), "x": round(current_focus[0], 2), "y": round(current_focus[1], 2)})
    return keyframes, meta
