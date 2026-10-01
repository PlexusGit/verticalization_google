"""Runtime configuration for the v10 autoframe pipeline.

The worker builds one :class:`Config` per request.  Tuning defaults match the
v10 prototype documented in ``face_detection_v_10/README.md``; only I/O paths,
device, and encoder vary per deployment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
WORKER_ROOT = PACKAGE_ROOT.parent
MODELS_DIR = WORKER_ROOT / "models"
ASD_DIR = WORKER_ROOT / "third_party" / "LR-ASD"

_TRUE = {"1", "true", "yes", "on"}


@dataclass
class Config:
    # Entrada / saída (preenchidos por request; workdir é sempre temporário)
    video: str = ""
    start: float = 0.0          # segundos
    duration: float = 0.0       # 0 = até o fim
    workdir: str = ""           # onde ficam analysis.json / plan.json
    output_w: int = 1080
    output_h: int = 1920

    # Detecção de rosto (YOLO-face)
    face_model: str = str(MODELS_DIR / "yolov11n-face.pt")
    face_conf: float = 0.5
    face_imgsz: int = 640
    face_batch: int = 16        # frames por chamada do detector

    # Tracking (IoU, por cena)
    track_iou: float = 0.4
    track_max_gap: int = 10        # frames sem detecção antes de encerrar o track
    track_min_len: int = 10        # frames

    # Active Speaker Detection (LR-ASD)
    asd_weights: str = str(ASD_DIR / "weight" / "finetuning_TalkSet.model")
    asd_crop_scale: float = 0.40
    asd_durations: tuple = (1, 1, 1, 2, 2, 2, 3, 3, 4, 5, 6)
    asd_low_conf: float = 1.0      # |logit| abaixo disso -> mistura com fallback visual

    # Fallback visual (FaceMesh: variação da abertura da boca)
    visual_hist: int = 10
    visual_var_thr: float = 0.008
    visual_min_open: float = 0.10

    # Audio VAD simples (RMS)
    vad_window: float = 0.15
    vad_rel_thr: float = 0.15      # fração do percentil 95 do RMS

    # Planejamento de câmera
    speak_thr: float = 0.5
    confirm_sec: float = 0.4       # tempo que o novo falante precisa dominar
    min_shot_sec: float = 1.5      # plano mínimo antes de trocar
    cut_dist_ratio: float = 0.20   # > isso da largura -> corte em vez de pan
    pan_sec: float = 0.45          # duração do pan entre alvos próximos
    zoom_factor: float = 3.2       # crop_h = face_h * zoom_factor
    min_crop_h_ratio: float = 0.55 # não deixa o crop menor que isso da altura
    headroom: float = 0.42         # posição vertical do centro do rosto no crop
    static_std_ratio: float = 0.012  # std do centro < isso*largura -> plano fixo
    dead_zone_ratio: float = 0.02
    smooth_sigma_sec: float = 0.35
    multi_mode: str = "cut"        # "cut" | "split" (split ainda não exposto)
    split_window_sec: float = 6.0  # janela para considerar diálogo ativo (split)
    no_face_mode: str = "center"   # "center" | "blur"

    # Render
    device: str = "cpu"            # resolvido por resolve_device()
    encoder: str = "libx264"       # resolvido por resolve_encoder()
    bitrate: str = "8M"
    render_timeout: int = 3300     # abaixo do timeout do Cloud Run (3600s)
    debug: bool = False            # vídeo lado a lado; apenas uso local

    def to_dict(self):
        d = asdict(self)
        d["asd_durations"] = list(self.asd_durations)
        return d

    @property
    def work(self) -> Path:
        p = Path(self.workdir or ".").resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tag(self) -> str:
        base = Path(self.video).stem or "video"
        return f"{base}_{int(self.start)}s_{int(self.duration) or 'full'}"


def _torch_device(kind: str) -> bool:
    try:
        import torch
        if kind == "cuda":
            return bool(torch.cuda.is_available())
        if kind == "mps":
            return bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
    except Exception:
        return False
    return False


def resolve_device() -> str:
    """Pick the inference device for this worker.

    ``AUTOFRAME_DEVICE`` forces a device when set.  Otherwise the shared
    ``TRANSCODE_USE_GPU`` flag governs GPU usage: ``1`` prefers CUDA (Cloud Run
    L4), then MPS (local macOS), falling back to CPU with a warning.  ``0``
    always selects CPU.
    """
    forced = os.getenv("AUTOFRAME_DEVICE", "").strip().lower()
    if forced in {"cpu", "cuda", "mps"}:
        return forced
    if os.getenv("TRANSCODE_USE_GPU", "0").strip().lower() not in _TRUE:
        return "cpu"
    if _torch_device("cuda"):
        return "cuda"
    if _torch_device("mps"):
        return "mps"
    import logging
    logging.getLogger(__name__).warning(
        "TRANSCODE_USE_GPU=1 but no CUDA/MPS device is available; using CPU"
    )
    return "cpu"


def _encoder_available(name: str) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
        return name in out.stdout
    except Exception:
        return False


def resolve_encoder(device: str | None = None) -> str:
    """Pick the H.264 encoder for the composed rawvideo stream.

    ``AUTOFRAME_ENCODER`` forces an encoder.  Otherwise NVENC is used when a
    CUDA device is active and the ffmpeg build supports it, VideoToolbox on
    local macOS, and libx264 everywhere else.
    """
    forced = os.getenv("AUTOFRAME_ENCODER", "").strip()
    if forced:
        return forced
    device = device or resolve_device()
    if device == "cuda" and shutil.which("nvidia-smi") and _encoder_available("h264_nvenc"):
        return "h264_nvenc"
    if sys.platform == "darwin" and _encoder_available("h264_videotoolbox"):
        return "h264_videotoolbox"
    return "libx264"
