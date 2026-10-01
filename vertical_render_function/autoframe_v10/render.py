"""Passe 3: aplica o plano -> vídeo vertical + trilha de áudio do original.

A composição por frame (crop/zoom/pan/split) acontece em OpenCV e o vídeo
compositor é enviado como rawvideo BGR24 ao ffmpeg, que aplica legendas
(ASS), logo e títulos — via ``overlay_builder`` fornecido pelo caller — e
muxa o áudio do arquivo de origem em uma única codificação.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from .config import Config

logger = logging.getLogger(__name__)

COLORS = [(255, 120, 0), (0, 200, 255), (120, 255, 0), (255, 0, 200), (0, 120, 255), (200, 200, 0), (255, 255, 255)]


def ffmpeg_writer(path, w, h, fps, cfg: Config, audio_src=None, start=0.0, duration=0.0,
                  filter_graph=None, final_label=None, logo_path=None, stderr_path=None):
    """Start an ffmpeg encoder fed by a bgr24 rawvideo pipe.

    Input order is fixed: ``0`` = composed frames (pipe), ``1`` = source media
    (audio + optional filter access), ``2`` = logo image (optional, a single
    frame; the overlay filter repeats it without ``-loop``).  ``filter_graph``
    (when provided) operates on ``[0:v]`` and must end at ``[final_label]``.
    """
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{w}x{h}", "-r", f"{fps:g}", "-i", "pipe:0"]
    if audio_src:
        cmd += ["-ss", f"{start:.3f}"]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-i", audio_src]
    if logo_path:
        cmd += ["-i", logo_path]
    if filter_graph:
        cmd += ["-filter_complex", filter_graph, "-map", f"[{final_label}]"]
    else:
        cmd += ["-map", "0:v:0"]
    if audio_src:
        cmd += ["-map", "1:a:0?", "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-c:v", cfg.encoder, "-b:v", cfg.bitrate, "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(path)]
    err = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
    return proc, err


def crop_resize(frame, crop, out_w, out_h):
    x, y, w, h = crop
    H, W = frame.shape[:2]
    x1, y1 = int(round(max(0, x))), int(round(max(0, y)))
    x2, y2 = int(round(min(W, x + w))), int(round(min(H, y + h)))
    if x2 <= x1 or y2 <= y1:
        return cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)
    return cv2.resize(frame[y1:y2, x1:x2], (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)


def blur_fit(frame, out_w, out_h):
    bg = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    bg = cv2.GaussianBlur(bg, (0, 0), 25)
    H, W = frame.shape[:2]
    s = out_w / W
    fg = cv2.resize(frame, (out_w, int(H * s)), interpolation=cv2.INTER_LANCZOS4)
    y0 = (out_h - fg.shape[0]) // 2
    bg[y0:y0 + fg.shape[0]] = fg
    return bg


def compose(frame, fr, cfg: Config):
    ow, oh = cfg.output_w, cfg.output_h
    if fr["layout"] == "blur":
        return blur_fit(frame, ow, oh)
    if fr["layout"] == "split" and "crop_top" in fr and "crop_bottom" in fr:
        top = crop_resize(frame, fr["crop_top"], ow, oh // 2)
        bot = crop_resize(frame, fr["crop_bottom"], ow, oh - oh // 2)
        out = np.vstack([top, bot])
        cv2.line(out, (0, oh // 2), (ow, oh // 2), (20, 20, 20), 6)
        return out
    if fr["layout"] == "single":
        return crop_resize(frame, fr["crop"], ow, oh)
    # "center" (sem rosto) e qualquer layout desconhecido -> crop central
    x, y, w, h = fr["crop"]
    return crop_resize(frame, [x, y, w, h], ow, oh)


def draw_debug(frame, f, fr, analysis, plan_fr_idx, cfg: Config):
    dbg = frame.copy()
    H, W = dbg.shape[:2]
    for t in analysis["tracks"]:
        if f < t["frames"][0] or f > t["frames"][-1]:
            continue
        i = f - t["frames"][0]
        x1, y1, x2, y2 = [int(v) for v in t["boxes"][i]]
        col = COLORS[t["id"] % len(COLORS)]
        sp = t["speak"][i]
        thick = 3 if t["id"] == fr.get("track") else 1
        cv2.rectangle(dbg, (x1, y1), (x2, y2), col, thick)
        bh = int((y2 - y1) * sp)
        cv2.rectangle(dbg, (x2 + 2, y2 - bh), (x2 + 8, y2), (0, 255, 0) if sp >= cfg.speak_thr else (0, 0, 255), -1)
        cv2.putText(dbg, f"t{t['id']} {sp:.2f} L{t['asd_logit'][i]:+.1f} V{t['visual'][i]:.1f}", (x1, max(12, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, col, 1)
    if fr["layout"] == "split":
        for key, col in (("crop_top", (0, 255, 255)), ("crop_bottom", (255, 255, 0))):
            if key in fr:
                x, y, w, h = [int(round(v)) for v in fr[key]]
                cv2.rectangle(dbg, (x, y), (x + w, y + h), col, 2)
    else:
        x, y, w, h = [int(round(v)) for v in fr["crop"]]
        cv2.rectangle(dbg, (x, y), (x + w, y + h), (0, 255, 255), 2)
    vad = analysis["vad"][plan_fr_idx]
    txt = f"{f / analysis['fps']:6.2f}s  scene {fr['scene']}  target t{fr['track']}  {fr['mode']}  {fr['layout']}  vad={'on' if vad else 'off'} {fr.get('trans', '')}"
    cv2.rectangle(dbg, (0, H - 22), (W, H), (0, 0, 0), -1)
    cv2.putText(dbg, txt, (6, H - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    return dbg


def _run_writer(path, w, h, fps, cfg: Config, plan_doc, *, start, duration,
                filter_graph, final_label, logo_path, stderr_path, analysis):
    """Compose every planned frame into the ffmpeg pipe; raise on failure."""
    writer = None
    dbg_writer = None
    err_handles = []
    try:
        writer, err = ffmpeg_writer(path, w, h, fps, cfg, cfg.video, start, duration,
                                    filter_graph, final_label, logo_path, stderr_path)
        err_handles.append(err)
        dbg_err = None
        if cfg.debug and analysis:
            dbg_path = cfg.work / f"{cfg.tag}_debug.mp4"
            dbg_h = H * 2
            out_small_w = int(w * dbg_h / h)
            dbg_w = W * 2 + out_small_w
            dbg_w += dbg_w % 2
            dbg_writer, dbg_err = ffmpeg_writer(dbg_path, dbg_w, dbg_h, fps, cfg,
                                                cfg.video, start, duration)
            err_handles.append(dbg_err)

        cap = cv2.VideoCapture(cfg.video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, plan_doc["start_frame"])
        n = plan_doc["end_frame"] - plan_doc["start_frame"]
        t0 = time.time()
        frames_written = 0
        try:
            for i in range(n):
                ok, frame = cap.read()
                if not ok:
                    logger.warning("v10.render: fonte terminou no frame %d de %d", i, n)
                    break
                fr = plan_doc["frames"][i]
                out = compose(frame, fr, cfg)
                writer.stdin.write(out.tobytes())
                frames_written += 1
                if dbg_writer is not None:
                    W_src = frame.shape[1]
                    left = cv2.resize(draw_debug(frame, plan_doc["start_frame"] + i, fr, analysis, i, cfg),
                                      (W_src * 2, dbg_h), interpolation=cv2.INTER_NEAREST)
                    right = cv2.resize(out, (out_small_w, dbg_h), interpolation=cv2.INTER_AREA)
                    canvas = np.zeros((dbg_h, dbg_w, 3), np.uint8)
                    canvas[:, :W_src * 2] = left
                    canvas[:, W_src * 2:W_src * 2 + out_small_w] = right
                    dbg_writer.stdin.write(canvas.tobytes())
                if i % 600 == 0 and i:
                    logger.info("v10.render %d/%d (%.1f fps)", i, n, i / (time.time() - t0))
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError(f"ffmpeg encerrado durante o envio de frames: {exc}") from exc
        finally:
            cap.release()
            if writer.stdin:
                writer.stdin.close()
            if dbg_writer is not None and dbg_writer.stdin:
                dbg_writer.stdin.close()
        try:
            rc = writer.wait(timeout=cfg.render_timeout)
        except subprocess.TimeoutExpired:
            writer.kill()
            raise RuntimeError("ffmpeg render timed out")
        if rc != 0:
            tail = Path(stderr_path).read_bytes()[-1500:].decode("utf-8", "replace") if stderr_path else ""
            raise RuntimeError(f"ffmpeg render failed (rc={rc}): {tail}")
        if dbg_writer is not None:
            dbg_writer.wait(timeout=cfg.render_timeout)
        if frames_written == 0:
            raise RuntimeError("nenhum frame foi composto")
        return frames_written
    finally:
        for err in err_handles:
            try:
                err.close()
            except Exception:
                pass


def render_plan(cfg: Config, plan_doc: dict, output_path, *, overlay_builder=None,
                logo_path=None, analysis: dict | None = None) -> dict:
    """Compose a per-frame plan into ``output_path`` (1080x1920 H.264 + AAC).

    ``overlay_builder(input_label, logo_input_index)`` returns
    ``(filters, final_label)`` — supplied by the caller (the worker wires it to
    ``renderer.build_overlay_filters``) so captions/logo/title land in the same
    encode.  Accelerated encoders fall back to libx264 once on failure.
    """
    fps, W, H = plan_doc["fps"], plan_doc["width"], plan_doc["height"]
    sf, ef = plan_doc["start_frame"], plan_doc["end_frame"]
    n = ef - sf
    if n <= 0:
        raise RuntimeError("plano vazio: nenhum frame a renderizar")
    start_sec, dur_sec = sf / fps, n / fps

    filter_graph = final_label = None
    logo_index = 2 if logo_path else None   # inputs: 0=pipe, 1=source, 2=logo
    if overlay_builder:
        filters, final_label = overlay_builder("0:v", logo_index)
        if filters:
            filter_graph = ";".join(filters)

    stderr_path = str(cfg.work / f"{cfg.tag}_ffmpeg.log")
    attempts = [cfg.encoder] if cfg.encoder == "libx264" else [cfg.encoder, "libx264"]
    last_exc = None
    for encoder in attempts:
        cfg_enc = cfg
        if encoder != cfg.encoder:
            import dataclasses
            cfg_enc = dataclasses.replace(cfg, encoder=encoder)
        try:
            frames = _run_writer(output_path, cfg.output_w, cfg.output_h, fps, cfg_enc,
                                 plan_doc, start=start_sec, duration=dur_sec,
                                 filter_graph=filter_graph, final_label=final_label,
                                 logo_path=logo_path, stderr_path=stderr_path,
                                 analysis=analysis)
            logger.info("v10.render saída -> %s (%d frames, encoder=%s)", output_path, frames, encoder)
            return {"frames": frames, "encoder": encoder}
        except Exception as exc:
            last_exc = exc
            if encoder != attempts[-1]:
                logger.warning("v10.render encoder %s falhou; tentando libx264 (%s)",
                               encoder, str(exc)[:300])
                continue
            raise
    raise last_exc
