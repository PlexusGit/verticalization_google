"""Process-level model registry for the v10 pipeline.

Worker concurrency is pinned to one request per container, so a single lazily
loaded model set is shared by every render served by the instance instead of
paying the load cost per request.  Models are keyed by (path, device) so a
configuration change recreates them deterministically.
"""
from __future__ import annotations

import logging
import sys

from .config import ASD_DIR, Config

logger = logging.getLogger(__name__)

_face_detector: dict | None = None
_asd_scorer: dict | None = None
_mouth_scorer: dict | None = None


def face_detector(cfg: Config):
    """YOLO-face detector, loaded once per (model, device)."""
    global _face_detector
    key = (cfg.face_model, cfg.device)
    if _face_detector is None or _face_detector["key"] != key:
        from ultralytics import YOLO

        logger.info("Carregando detector YOLO-face model=%s device=%s", cfg.face_model, cfg.device)
        _face_detector = {"key": key, "model": YOLO(cfg.face_model)}
    return _face_detector["model"]


def asd_scorer(cfg: Config):
    """LR-ASD active-speaker model, loaded once per (weights, device)."""
    global _asd_scorer
    key = (cfg.asd_weights, cfg.device)
    if _asd_scorer is None or _asd_scorer["key"] != key:
        from .analyze import LRASD

        logger.info("Carregando LR-ASD weights=%s device=%s", cfg.asd_weights, cfg.device)
        _asd_scorer = {"key": key, "model": LRASD(cfg)}
    return _asd_scorer["model"]


def mouth_scorer(cfg: Config):
    """FaceMesh mouth-movement scorer; ``None`` when MediaPipe is absent.

    MediaPipe is optional: Linux/amd64 and macOS workers ship it, but local
    Python 3.13 environments cannot install it.  Without it the visual
    fallback scores zero and LR-ASD logits drive speaker selection alone.
    """
    global _mouth_scorer
    key = True
    if _mouth_scorer is None or _mouth_scorer["key"] != key:
        from .analyze import MouthScorer

        scorer = MouthScorer(cfg)
        if scorer.mesh is None:
            logger.warning("MediaPipe indisponível; fallback visual desativado")
        _mouth_scorer = {"key": key, "model": scorer}
    return _mouth_scorer["model"]


def reset() -> None:
    """Drop cached models (tests and device changes)."""
    global _face_detector, _asd_scorer, _mouth_scorer
    _face_detector = _asd_scorer = _mouth_scorer = None
    # Ensure a subsequent ASD load re-imports the vendored module cleanly.
    for name in [m for m in sys.modules if m == "model" or m.startswith("model.")]:
        del sys.modules[name]
