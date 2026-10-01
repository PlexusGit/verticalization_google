# ===== face_vertical_shorts_v10.py =====
"""v10: pipeline em 3 passes (analise -> plano de camera -> render).

Uso:
  python face_detection.py all     --video "Enchente Nepal.mp4" --start 0 --duration 90
  python face_detection.py analyze --video ... (so passe 1)
  python face_detection.py plan    --video ... (reusa analysis.json)
  python face_detection.py render  --video ... (reusa plan.json)

Os passes vivem no pacote de producao ``autoframe_v10`` (um diretorio acima);
este arquivo e apenas o CLI de experimentacao local.
"""
import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoframe_v10.config import Config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")


def build_cfg(args) -> Config:
    cfg = Config()
    for f in dataclasses.fields(Config):
        v = getattr(args, f.name, None)
        if v is not None:
            setattr(cfg, f.name, v)
    return cfg


def main():
    p = argparse.ArgumentParser()
    p.add_argument("step", choices=["analyze", "plan", "render", "all"])
    for f in dataclasses.fields(Config):
        if f.name == "asd_durations":
            continue
        typ = type(f.default) if not isinstance(f.default, bool) else lambda s: s.lower() in ("1", "true", "yes")
        p.add_argument(f"--{f.name}", type=typ, default=None)
    args = p.parse_args()
    cfg = build_cfg(args)

    analysis = plan = None
    if args.step in ("analyze", "all"):
        from autoframe_v10.analyze import analyze
        analysis = analyze(cfg)
    if args.step in ("plan", "all"):
        from autoframe_v10.plan import plan as make_plan
        if analysis is None:
            analysis = json.loads((cfg.work / f"{cfg.tag}_analysis.json").read_text())
        plan = make_plan(cfg, analysis)
    if args.step in ("render", "all"):
        from autoframe_v10.render import render_plan
        if analysis is None:
            analysis = json.loads((cfg.work / f"{cfg.tag}_analysis.json").read_text())
        if plan is None:
            plan = json.loads((cfg.work / f"{cfg.tag}_plan.json").read_text())
        out_path = Path(cfg.workdir or ".") / f"{cfg.tag}_vertical.mp4"
        render_plan(cfg, plan, out_path, analysis=analysis)


if __name__ == "__main__":
    main()
