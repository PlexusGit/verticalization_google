"""Passe 1: cenas, rostos, tracks, ASD (LR-ASD) + fallback visual -> analysis.json"""
from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .config import ASD_DIR, Config
from . import models

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- utils
def iou(a, b):
    xa, ya = max(a[0], b[0]), max(a[1], b[1])
    xb, yb = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, xb - xa) * max(0, yb - ya)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


# ---------------------------------------------------------------- cenas
def detect_scenes(cfg: Config, start_frame, end_frame, fps):
    from scenedetect import open_video, SceneManager, ContentDetector, FrameTimecode

    video = open_video(cfg.video)
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=27.0, min_scene_len=max(2, int(fps * 0.5))))
    video.seek(FrameTimecode(start_frame, fps))
    sm.detect_scenes(video, end_time=FrameTimecode(end_frame, fps), show_progress=False)
    scenes = [[s.get_frames(), e.get_frames()] for s, e in sm.get_scene_list()]
    if not scenes:
        scenes = [[start_frame, end_frame]]
    scenes[0][0] = start_frame
    scenes[-1][1] = end_frame
    return scenes


# ---------------------------------------------------------------- audio
def extract_audio(cfg: Config, start, duration):
    """Mono 16kHz PCM for VAD/MFCC. Silent zeros when the source has no audio."""
    from scipy.io import wavfile

    n_samples = int(round(duration * 16000)) if duration else 0
    tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=str(cfg.work))
    tf.close()
    try:
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}"]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-i", cfg.video, "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", tf.name]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {(proc.stderr or '')[-400:]}")
        sr, audio = wavfile.read(tf.name)
        if sr != 16000 or len(audio) == 0:
            raise RuntimeError("audio extraction produced no samples")
        return sr, audio.astype(np.float32)
    except Exception as exc:
        logger.warning("Sem áudio extraível (%s); ASD/VAD usam silêncio", exc)
        return 16000, np.zeros(max(1, n_samples), dtype=np.float32)
    finally:
        Path(tf.name).unlink(missing_ok=True)


def audio_vad(audio, sr, fps, n_frames, cfg: Config):
    half = max(1, int(cfg.vad_window * sr / 2))
    rms = np.zeros(n_frames, dtype=np.float32)
    centers = np.arange(n_frames) / fps * sr
    # Chunked RMS: janela centrada no instante de cada frame de vídeo.
    idx = np.clip(centers.astype(int), 0, len(audio))
    lo = np.clip(idx - half, 0, len(audio))
    hi = np.clip(idx + half, 0, len(audio))
    for i in range(n_frames):
        chunk = audio[lo[i]:hi[i]]
        rms[i] = math.sqrt(float(np.mean(chunk * chunk))) if len(chunk) else 0.0
    rms /= 32768.0
    thr = max(0.005, cfg.vad_rel_thr * float(np.percentile(rms, 95)))
    return rms, (rms > thr)


# ---------------------------------------------------------------- detecção + tracking
def detect_faces(cfg: Config, start_frame, end_frame):
    model = models.face_detector(cfg)
    cap = cv2.VideoCapture(cfg.video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    dets = []
    batch_frames, batch_ids = [], []
    t0 = time.time()
    total = end_frame - start_frame
    for f in range(start_frame, end_frame):
        ok, frame = cap.read()
        if not ok:
            break
        batch_frames.append(frame)
        batch_ids.append(f)
        if len(batch_frames) >= cfg.face_batch:
            _run_face_batch(model, cfg, batch_frames, batch_ids, dets)
            batch_frames, batch_ids = [], []
        if (f - start_frame) % 600 == 0 and f > start_frame:
            el = time.time() - t0
            logger.info("  faces %d/%d (%.1f fps)", f - start_frame, total, (f - start_frame) / el)
    if batch_frames:
        _run_face_batch(model, cfg, batch_frames, batch_ids, dets)
    cap.release()
    return dets


def _run_face_batch(model, cfg: Config, frames, frame_ids, dets):
    results = model(frames, imgsz=cfg.face_imgsz, conf=cfg.face_conf, verbose=False, device=cfg.device)
    for f, r in zip(frame_ids, results):
        boxes = r.boxes.xyxy.cpu().numpy().tolist()
        confs = r.boxes.conf.cpu().numpy().tolist()
        dets.append([{"frame": f, "bbox": b, "conf": c} for b, c in zip(boxes, confs)])


def track_scene(cfg: Config, scene_dets):
    """Tracking greedy por IoU (estilo TalkNet), retorna tracks com boxes interpolados."""
    from scipy.interpolate import interp1d

    frames_left = [list(fr) for fr in scene_dets]
    tracks = []
    while True:
        track = []
        for fr in frames_left:
            for face in list(fr):
                if not track:
                    track.append(face)
                    fr.remove(face)
                elif face["frame"] - track[-1]["frame"] <= cfg.track_max_gap:
                    if iou(face["bbox"], track[-1]["bbox"]) > cfg.track_iou:
                        track.append(face)
                        fr.remove(face)
                        break
        if not track:
            break
        if len(track) > cfg.track_min_len:
            fn = np.array([f["frame"] for f in track])
            bb = np.array([f["bbox"] for f in track])
            fi = np.arange(fn[0], fn[-1] + 1)
            bbi = np.stack([interp1d(fn, bb[:, j])(fi) for j in range(4)], axis=1)
            tracks.append({"frames": fi.tolist(), "boxes": bbi.round(1).tolist()})
    return tracks


# ---------------------------------------------------------------- ASD
class LRASD:
    def __init__(self, cfg: Config):
        import torch

        if str(ASD_DIR) not in sys.path:
            sys.path.insert(0, str(ASD_DIR))
        from model.Model import ASD_Model
        from loss import lossAV

        # LR-ASD uses 3D pooling, which PyTorch does not implement on MPS. The
        # model is tiny (0.84M params), so it runs on CPU while YOLO keeps the
        # accelerator.
        self.dev = "cpu" if cfg.device == "mps" else cfg.device
        self.model = ASD_Model().to(self.dev).eval()
        self.head = lossAV().to(self.dev).eval()
        sd = torch.load(cfg.asd_weights, map_location="cpu", weights_only=False)
        self.model.load_state_dict({k[6:]: v for k, v in sd.items() if k.startswith("model.")})
        self.head.load_state_dict({k[7:]: v for k, v in sd.items() if k.startswith("lossAV.")})
        self.durations = cfg.asd_durations

    def score(self, mfcc, faces):
        """mfcc: (Na,13) a 100fps; faces: (Nv,112,112) a 25fps -> logits por frame visual."""
        import torch

        n_out = faces.shape[0]
        length = min((mfcc.shape[0] - mfcc.shape[0] % 4) / 100, n_out / 25)
        na, nv = int(round(length * 100)), int(round(length * 25))
        if nv < 1:
            return np.zeros(n_out, dtype=np.float32)
        mfcc, faces = mfcc[:na], faces[:nv]
        all_scores = []
        with torch.no_grad():
            for d in self.durations:
                scores = []
                for i in range(int(math.ceil(length / d))):
                    a = torch.FloatTensor(mfcc[i * d * 100:(i + 1) * d * 100]).unsqueeze(0).to(self.dev)
                    v = torch.FloatTensor(faces[i * d * 25:(i + 1) * d * 25]).unsqueeze(0).to(self.dev)
                    if a.shape[1] < 4 or v.shape[1] < 1:
                        continue
                    ea = self.model.forward_audio_frontend(a)
                    ev = self.model.forward_visual_frontend(v)
                    out = self.model.forward_audio_visual_backend(ea, ev)
                    scores.extend(self.head(out).tolist())
                if len(scores) == nv:
                    all_scores.append(scores)
        if not all_scores:
            return np.zeros(n_out, dtype=np.float32)
        s = np.mean(np.array(all_scores), axis=0)
        if len(s) < n_out:
            s = np.pad(s, (0, n_out - len(s)), mode="edge")
        return s[:n_out].astype(np.float32)


def asd_face_crop(frame, cx, cy, s, cs):
    """Recorte igual ao demo do LR-ASD: quadrado com padding cs, 224x224 -> gray -> 112 central."""
    bsi = int(s * (1 + 2 * cs))
    pad = np.pad(frame, ((bsi, bsi), (bsi, bsi), (0, 0)), "constant", constant_values=110)
    my, mx = cy + bsi, cx + bsi
    face = pad[int(my - s):int(my + s * (1 + 2 * cs)), int(mx - s * (1 + cs)):int(mx + s * (1 + cs))]
    if face.size == 0:
        return np.full((112, 112), 110, np.uint8)
    face = cv2.cvtColor(cv2.resize(face, (224, 224)), cv2.COLOR_BGR2GRAY)
    return face[56:168, 56:168]


# ---------------------------------------------------------------- fallback visual (FaceMesh)
class MouthScorer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        try:
            import mediapipe as mp
            self.mesh = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=False,
                min_detection_confidence=0.3, min_tracking_confidence=0.3)
        except Exception:
            self.mesh = None

    def openness(self, face_bgr):
        if self.mesh is None:
            return None
        res = self.mesh.process(cv2.cvtColor(face_bgr, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            return None
        lm = res.multi_face_landmarks[0].landmark
        h, w = face_bgr.shape[:2]
        p = lambda i: (lm[i].x * w, lm[i].y * h)
        top, bot, l, r = p(13), p(14), p(61), p(291)
        return math.hypot(top[0] - bot[0], top[1] - bot[1]) / (math.hypot(l[0] - r[0], l[1] - r[1]) + 1e-6)

    def series_to_score(self, opens):
        """Transforma a série de abertura em score 0-1 por frame (variação em janela)."""
        cfg = self.cfg
        out, hist = [], deque(maxlen=cfg.visual_hist)
        for o in opens:
            if o is None:
                out.append(0.0)
                continue
            hist.append(o)
            var = (max(hist) - min(hist)) if len(hist) >= 2 else 0.0
            speaking = var > cfg.visual_var_thr and o > cfg.visual_min_open
            out.append(min(1.0, var / (cfg.visual_var_thr * 4)) if speaking else 0.0)
        return out


# ---------------------------------------------------------------- pipeline
def analyze(cfg: Config) -> dict:
    from scipy import signal
    from scipy.interpolate import interp1d
    import python_speech_features

    cap = cv2.VideoCapture(cfg.video)
    fps = cap.get(cv2.CAP_PROP_FPS)
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if not fps or fps <= 0 or not W or not H:
        raise RuntimeError(f"não foi possível ler metadados do vídeo (fps={fps}, {W}x{H})")
    start_frame = int(round(cfg.start * fps))
    end_frame = total if not cfg.duration else min(total, int(round((cfg.start + cfg.duration) * fps)))
    n = end_frame - start_frame
    if n <= 0:
        raise RuntimeError(f"trecho vazio: frames {start_frame}-{end_frame}")
    logger.info("v10.analyze video=%dx%d @ %.3ffps frames=%d-%d (%d)",
                W, H, fps, start_frame, end_frame, n)

    t0 = time.time()
    scenes = detect_scenes(cfg, start_frame, end_frame, fps)
    logger.info("v10.analyze cenas=%d (%.1fs)", len(scenes), time.time() - t0)

    t0 = time.time()
    sr, audio = extract_audio(cfg, cfg.start, n / fps)
    mfcc = python_speech_features.mfcc(audio, sr, numcep=13, winlen=0.025, winstep=0.010)
    rms, vad = audio_vad(audio, sr, fps, n, cfg)
    logger.info("v10.analyze audio/VAD ok (%.1fs)", time.time() - t0)

    t0 = time.time()
    dets = detect_faces(cfg, start_frame, end_frame)
    logger.info("v10.analyze faces frames=%d (%.1fs)", len(dets), time.time() - t0)

    tracks = []
    for si, (s0, s1) in enumerate(scenes):
        for t in track_scene(cfg, dets[s0 - start_frame:s1 - start_frame]):
            t["id"] = len(tracks)
            t["scene"] = si
            tracks.append(t)
    logger.info("v10.analyze tracks=%d", len(tracks))

    t0 = time.time()
    asd = models.asd_scorer(cfg)
    mouth = models.mouth_scorer(cfg)
    # centros/tamanhos suavizados (medfilt) por track, como no demo do LR-ASD
    for t in tracks:
        b = np.array(t["boxes"])
        s = np.maximum(b[:, 3] - b[:, 1], b[:, 2] - b[:, 0]) / 2
        k = min(13, len(s) - (1 - len(s) % 2))
        k = k if k >= 3 else 1
        t["_s"] = signal.medfilt(s, k) if k > 1 else s
        t["_cx"] = signal.medfilt((b[:, 0] + b[:, 2]) / 2, k) if k > 1 else (b[:, 0] + b[:, 2]) / 2
        t["_cy"] = signal.medfilt((b[:, 1] + b[:, 3]) / 2, k) if k > 1 else (b[:, 1] + b[:, 3]) / 2
        t["_faces"] = {}   # frame -> crop 112x112 (para ASD, amostrado depois a 25fps)
        t["_opens"] = []

    active = {}
    for t in tracks:
        for i, f in enumerate(t["frames"]):
            active.setdefault(f, []).append((t, i))

    cap = cv2.VideoCapture(cfg.video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    for f in range(start_frame, end_frame):
        ok, frame = cap.read()
        if not ok:
            break
        for t, i in active.get(f, ()):  # crops ASD + FaceMesh para cada track vivo
            cx, cy, s = t["_cx"][i], t["_cy"][i], t["_s"][i]
            t["_faces"][f] = asd_face_crop(frame, cx, cy, s, cfg.asd_crop_scale)
            x1, y1 = int(max(0, cx - s * 1.3)), int(max(0, cy - s * 1.3))
            x2, y2 = int(min(W, cx + s * 1.3)), int(min(H, cy + s * 1.3))
            crop = frame[y1:y2, x1:x2]
            t["_opens"].append(mouth.openness(cv2.resize(crop, (224, 224))) if crop.size else None)
        if (f - start_frame) % 600 == 0 and f > start_frame:
            logger.info("v10.analyze crops %d/%d", f - start_frame, n)
    cap.release()

    for t in tracks:
        fr = np.array(t["frames"])
        t_start, t_end = fr[0] / fps, (fr[-1] + 1) / fps
        rel0 = t_start - start_frame / fps
        # visual a 25fps por amostragem no tempo
        times25 = np.arange(t_start, t_end, 1 / 25)
        idx = np.clip(np.round(times25 * fps).astype(int), fr[0], fr[-1])
        missing = [int(k) for k in idx if int(k) not in t["_faces"]]
        if missing:
            raise RuntimeError(f"track {t['id']}: {len(missing)} frames sem crop para ASD")
        faces = np.stack([t["_faces"][int(k)] for k in idx])
        a0 = int(round(rel0 * 100))
        a1 = a0 + int(round((t_end - t_start) * 100))
        a1 = min(a1, mfcc.shape[0])
        logits25 = asd.score(mfcc[a0:a1], faces)
        # volta para a taxa do vídeo
        fint = interp1d(times25[: len(logits25)], logits25, bounds_error=False,
                        fill_value=(logits25[0], logits25[-1]))
        logits = fint(fr / fps)
        visual = np.array(mouth.series_to_score(t["_opens"]))
        prob = sigmoid(logits)
        conf = np.abs(logits)
        w = np.clip(conf / cfg.asd_low_conf, 0, 1)      # 0 = sem confiança -> usa visual
        speak = w * prob + (1 - w) * (0.5 * prob + 0.5 * visual)
        gate = np.where(vad[np.clip(fr - start_frame, 0, n - 1)], 1.0, 0.3)
        t["asd_logit"] = np.asarray(logits).round(3).tolist()
        t["visual"] = visual.round(3).tolist()
        t["speak"] = (speak * gate).round(3).tolist()
        for key in ("_s", "_cx", "_cy", "_faces", "_opens"):
            del t[key]
    logger.info("v10.analyze ASD+visual ok (%.1fs)", time.time() - t0)

    out = {
        "config": cfg.to_dict(), "fps": fps, "width": W, "height": H,
        "start_frame": start_frame, "end_frame": end_frame,
        "scenes": scenes, "tracks": tracks,
        "audio_rms": rms.round(4).tolist(), "vad": vad.astype(int).tolist(),
    }
    path = cfg.work / f"{cfg.tag}_analysis.json"
    path.write_text(json.dumps(out))
    logger.info("v10.analyze analysis -> %s", path)
    return out
