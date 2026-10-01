"""Versioned camera-plan artifact contract.

A v10 plan is a per-frame structure far larger than a Firestore document, so it
travels between draft and final renders as a gzip-compressed JSON object in
GCS.  Its fingerprint binds the plan to the source object, the chapter bounds,
the output geometry, every planner/detector constant, and the model checksums —
any change silently regenerates the analysis instead of reusing a stale plan.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from .config import Config

AUTOFRAME_STRATEGY_VERSION = 10
PLAN_FORMAT = "autoframe-plan/1"

# Config fields that change the planned trajectory.  I/O paths, device, and
# encoder are intentionally excluded — they affect speed, not the plan.
_FINGERPRINT_FIELDS = (
    "face_conf", "face_imgsz",
    "track_iou", "track_max_gap", "track_min_len",
    "asd_crop_scale", "asd_low_conf",
    "visual_hist", "visual_var_thr", "visual_min_open",
    "vad_window", "vad_rel_thr",
    "speak_thr", "confirm_sec", "min_shot_sec",
    "cut_dist_ratio", "pan_sec", "zoom_factor", "min_crop_h_ratio",
    "headroom", "static_std_ratio", "dead_zone_ratio", "smooth_sigma_sec",
    "multi_mode", "split_window_sec", "no_face_mode",
    "output_w", "output_h",
)

_checksums: dict[str, str] = {}


def file_sha256(path: str) -> str | None:
    """SHA-256 of a model file, cached per process (models are immutable)."""
    if path in _checksums:
        return _checksums[path]
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    _checksums[path] = digest.hexdigest()
    return _checksums[path]


def plan_fingerprint(cfg: Config, *, source_gcs_path: str, source_generation: str | None,
                     trim_start: float, trim_end: float, source_width: int,
                     source_height: int) -> str:
    """Deterministic identity of everything a plan depends on."""
    inputs = {
        "format": PLAN_FORMAT,
        "strategy": AUTOFRAME_STRATEGY_VERSION,
        "source_gcs_path": source_gcs_path,
        "source_generation": str(source_generation) if source_generation is not None else None,
        "trim_start": round(float(trim_start), 3),
        "trim_end": round(float(trim_end), 3),
        "source_width": int(source_width),
        "source_height": int(source_height),
        "face_model": file_sha256(cfg.face_model),
        "asd_weights": file_sha256(cfg.asd_weights),
        "config": {name: getattr(cfg, name) for name in _FINGERPRINT_FIELDS},
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def dumps_plan(plan: dict) -> bytes:
    return gzip.compress(json.dumps(plan).encode(), compresslevel=6)


def loads_plan(blob: bytes) -> dict:
    plan = json.loads(gzip.decompress(blob).decode())
    if plan.get("format") != PLAN_FORMAT:
        raise ValueError("formato de plano desconhecido")
    if not isinstance(plan.get("frames"), list) or not plan["frames"]:
        raise ValueError("plano sem frames")
    return plan


def artifact_blob_path(video_id: str, chapter_id: str, fingerprint: str) -> str:
    return f"{video_id}/chapters/{chapter_id}/autoframe/v{AUTOFRAME_STRATEGY_VERSION}/{fingerprint}.plan.json.gz"


def write_local_plan(plan: dict, path: str | Path) -> None:
    Path(path).write_bytes(dumps_plan(plan))


def read_local_plan(path: str | Path) -> dict:
    return loads_plan(Path(path).read_bytes())
