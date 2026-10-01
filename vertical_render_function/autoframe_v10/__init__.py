"""v10 autoframe pipeline: analyze -> plan -> render.

Heavy dependencies (torch, ultralytics, scenedetect, mediapipe, scipy) are
imported lazily inside each module so this package stays importable in
environments that only run the legacy renderer.
"""
from __future__ import annotations

from .artifacts import AUTOFRAME_STRATEGY_VERSION  # noqa: F401
from .config import Config, resolve_device, resolve_encoder  # noqa: F401
