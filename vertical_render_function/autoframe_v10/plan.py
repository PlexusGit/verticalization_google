"""Passe 2: planejamento da câmera virtual a partir da análise -> plan.json

Doutrina: "não mova a menos que precise".
  1. Por cena, escolhe o track alvo por frame com histerese (falante ativo).
  2. Agrupa em segmentos (alvo fixo). Zoom fixo por segmento.
  3. Posição por segmento: static / pan / track (dead zone + suavização bidirecional).
  4. Entre segmentos da mesma cena: corte (longe) ou pan curto (perto).
Saída: por frame -> layout ("single"|"split"|"center"), crops e metadados de debug.
"""
from __future__ import annotations

import json
import logging
import time

import numpy as np
from scipy import signal
from scipy.ndimage import gaussian_filter1d

from .config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers
def track_arrays(track):
    b = np.array(track["boxes"], dtype=float)
    return {
        "frames": np.array(track["frames"]),
        "cx": (b[:, 0] + b[:, 2]) / 2, "cy": (b[:, 1] + b[:, 3]) / 2,
        "w": b[:, 2] - b[:, 0], "h": b[:, 3] - b[:, 1],
        "speak": np.array(track["speak"], dtype=float),
    }


def medfilt_odd(x, k):
    k = min(k, len(x) - (1 - len(x) % 2))
    return signal.medfilt(x, k) if k >= 3 else x


def dead_zone_hold(x, dz):
    out = np.empty_like(x)
    held = x[0]
    for i, v in enumerate(x):
        if abs(v - held) > dz:
            held = v
        out[i] = held
    return out


def ease(t):
    return 0.5 - 0.5 * np.cos(np.pi * np.clip(t, 0, 1))


# ---------------------------------------------------------------- seleção do alvo (histerese)
def choose_targets(tracks, s0, s1, fps, cfg: Config):
    """Retorna array (s1-s0,) com id do track alvo por frame (-1 = nenhum rosto)."""
    n = s1 - s0
    present = {t["id"]: np.full(n, np.nan) for t in tracks}
    size = {t["id"]: np.zeros(n) for t in tracks}
    for t in tracks:
        a = track_arrays(t)
        idx = a["frames"] - s0
        present[t["id"]][idx] = a["speak"]
        size[t["id"]][idx] = a["w"] * a["h"]

    ids = list(present.keys())
    if not ids:
        return np.full(n, -1)
    speak = np.array([present[i] for i in ids])          # (K, n) nan = ausente
    area = np.array([size[i] for i in ids])
    confirm = max(1, int(cfg.confirm_sec * fps))
    min_shot = int(cfg.min_shot_sec * fps)

    target = np.full(n, -1)
    cur, cur_since, pending, pend_count = None, 0, None, 0
    for f in range(n):
        vis = ~np.isnan(speak[:, f])
        if not vis.any():
            cur, pending, pend_count = None, None, 0
            continue
        sp = np.where(vis, np.nan_to_num(speak[:, f], nan=-1), -1)
        best = int(np.argmax(sp))
        speaking = sp[best] >= cfg.speak_thr
        if cur is None or not vis[cur]:
            # sem alvo (ou alvo sumiu): pega quem fala; senão o maior rosto
            cur = best if speaking else int(np.argmax(np.where(vis, area[:, f], -1)))
            cur_since, pending, pend_count = f, None, 0
        elif speaking and best != cur and sp[best] > sp[cur] + 0.15:
            if pending == best:
                pend_count += 1
            else:
                pending, pend_count = best, 1
            if pend_count >= confirm and f - cur_since >= min_shot:
                cur, cur_since, pending, pend_count = best, f, None, 0
        else:
            pending, pend_count = None, 0
        target[f] = ids[cur]
    return remove_short_runs(target, int(0.3 * fps))


def remove_short_runs(target, min_run):
    """Absorve trechos muito curtos no segmento anterior (evita flicker de 1-2 frames)."""
    t = target.copy()
    n = len(t)
    i = 0
    while i < n:
        j = i
        while j < n and t[j] == t[i]:
            j += 1
        if j - i < min_run and i > 0:
            t[i:j] = t[i - 1]
        i = j
    return t


def split_partner(tracks, s0, s1, fps, target, cfg: Config):
    """Para multi_mode=split: por frame, id de um 2o track em diálogo recente com o alvo (ou -1)."""
    n = s1 - s0
    partner = np.full(n, -1)
    win = int(cfg.split_window_sec * fps)
    spk = {}
    for t in tracks:
        a = track_arrays(t)
        arr = np.zeros(n, dtype=bool)
        arr[a["frames"] - s0] = a["speak"] >= cfg.speak_thr
        spk[t["id"]] = arr
    pres = {t["id"]: np.isin(np.arange(s0, s1), t["frames"]) for t in tracks}
    for f in range(n):
        tid = target[f]
        if tid < 0:
            continue
        lo, hi = max(0, f - win), min(n, f + win)
        best, best_cnt = -1, 0
        for oid in spk:
            if oid == tid or not pres[oid][f]:
                continue
            cnt = int(spk[oid][lo:hi].sum())
            if cnt > best_cnt and spk[tid][lo:hi].sum() > 0:
                best, best_cnt = oid, cnt
        if best_cnt >= int(0.5 * fps):
            partner[f] = best
    return partner


# ---------------------------------------------------------------- câmera por segmento
def crop_size_for(face_h, W, H, out_w, out_h, cfg: Config):
    ar = out_w / out_h
    ch = float(np.clip(face_h * cfg.zoom_factor, cfg.min_crop_h_ratio * H, H))
    cw = ch * ar
    if cw > W:
        cw, ch = W, W / ar
    return cw, ch


def plan_segment_path(a_frames, cx, cy, s0, seg, W, H, cw, ch, fps, cfg: Config):
    """Retorna (x, y) do canto do crop para cada frame do segmento [f0, f1)."""
    f0, f1 = seg
    n = f1 - f0
    # mapeia posição do rosto para todos os frames do segmento (extrapola bordas)
    fr = np.arange(f0, f1)
    cxs = np.interp(fr, a_frames, cx)
    cys = np.interp(fr, a_frames, cy)
    cxs, cys = medfilt_odd(cxs, 15), medfilt_odd(cys, 15)

    def path(c, size, limit):
        std = float(np.std(c))
        if n < fps * 0.5 or std < cfg.static_std_ratio * W:
            mode, p = "static", np.full(n, np.median(c))
        else:
            drift = abs(c[-1] - c[0])
            if drift > 2 * std and std < 0.06 * W:
                mode, p = "pan", np.linspace(np.median(c[:max(3, n // 8)]), np.median(c[-max(3, n // 8):]), n)
            else:
                mode = "track"
                p = dead_zone_hold(c, cfg.dead_zone_ratio * W)
                p = gaussian_filter1d(p, max(1.0, cfg.smooth_sigma_sec * fps), mode="nearest")
        return mode, p

    mode_x, px = path(cxs, cw, W)
    _, py = path(cys, ch, H)
    x = np.clip(px - cw / 2, 0, W - cw)
    y = np.clip(py - ch * cfg.headroom, 0, H - ch)
    return x, y, mode_x


# ---------------------------------------------------------------- plano
def plan(cfg: Config, analysis: dict) -> dict:
    fps, W, H = analysis["fps"], analysis["width"], analysis["height"]
    sf, ef = analysis["start_frame"], analysis["end_frame"]
    n = ef - sf
    tracks_by_id = {t["id"]: t for t in analysis["tracks"]}
    frames = [None] * n
    segments = []

    for si, (s0, s1) in enumerate(analysis["scenes"]):
        scene_tracks = [t for t in analysis["tracks"] if t["scene"] == si]
        target = choose_targets(scene_tracks, s0, s1, fps, cfg)
        partner = split_partner(scene_tracks, s0, s1, fps, target, cfg) if cfg.multi_mode == "split" else np.full(s1 - s0, -1)

        # segmentos de alvo constante
        segs, start = [], 0
        for f in range(1, s1 - s0 + 1):
            if f == s1 - s0 or target[f] != target[start]:
                segs.append((s0 + start, s0 + f, int(target[start])))
                start = f

        prev = None
        for (f0, f1, tid) in segs:
            if tid < 0:
                cw, ch = crop_size_for(H, W, H, cfg.output_w, cfg.output_h, cfg)
                for f in range(f0, f1):
                    frames[f - sf] = {"layout": cfg.no_face_mode, "crop": [(W - cw) / 2, (H - ch) / 2, cw, ch],
                                      "track": -1, "mode": "nobody", "scene": si}
                segments.append({"f0": f0, "f1": f1, "track": -1, "mode": "nobody", "scene": si})
                prev = None
                continue
            a = track_arrays(tracks_by_id[tid])
            m = (a["frames"] >= f0) & (a["frames"] < f1)
            face_h = float(np.median(a["h"][m])) if m.any() else float(np.median(a["h"]))
            cw, ch = crop_size_for(face_h, W, H, cfg.output_w, cfg.output_h, cfg)
            x, y, mode = plan_segment_path(a["frames"], a["cx"], a["cy"], sf, (f0, f1), W, H, cw, ch, fps, cfg)

            # transição a partir do segmento anterior (mesma cena)
            trans = "cut"
            if prev is not None:
                px, py, pcw, pch, ptid = prev
                dist = float(np.hypot(((px + pcw / 2) - (x[0] + cw / 2)) / W, ((py + pch / 2) - (y[0] + ch / 2)) / H))
                prev_frames, new_frames = set(tracks_by_id[ptid]["frames"]), set(a["frames"].tolist())
                coexist = (f0 - 1) in new_frames or f0 in prev_frames   # os dois rostos estavam na tela juntos
                if dist <= cfg.cut_dist_ratio and coexist:
                    trans = "pan"
                    k = min(len(x), max(2, int(cfg.pan_sec * fps)))
                    e = ease(np.linspace(0, 1, k))
                    x[:k] = px + (x[:k] - px) * e
                    y[:k] = py + (y[:k] - py) * e
                    cws = pcw + (cw - pcw) * e
                    chs = pch + (ch - pch) * e
                else:
                    cws = chs = None
            else:
                cws = chs = None

            for i, f in enumerate(range(f0, f1)):
                w_i = cws[i] if (cws is not None and i < len(cws)) else cw
                h_i = chs[i] if (chs is not None and i < len(chs)) else ch
                fr = {"layout": "single", "crop": [float(x[i]), float(y[i]), float(w_i), float(h_i)],
                      "track": tid, "mode": mode, "scene": si, "trans": trans if i == 0 else ""}
                pid = int(partner[f - s0])
                if pid >= 0:
                    fr["layout"] = "split"
                    fr["partner"] = pid
                frames[f - sf] = fr
            prev = (float(x[-1]), float(y[-1]), cw, ch, tid)
            segments.append({"f0": f0, "f1": f1, "track": tid, "mode": mode, "trans": trans, "scene": si,
                             "crop_wh": [cw, ch]})

    # crops do parceiro (split): metade superior/inferior, cada uma com aspecto out_w:(out_h/2)
    if cfg.multi_mode == "split":
        half_ar = cfg.output_w / (cfg.output_h / 2)
        cache = {}
        for i, fr in enumerate(frames):
            if fr is None or fr["layout"] != "split":
                continue
            f = sf + i
            # ordem estável: rosto mais alto na tela fica em cima (não troca quando muda o falante)
            ta, tb = fr["track"], fr["partner"]
            cy_a = np.mean(track_arrays(tracks_by_id[ta])["cy"])
            cy_b = np.mean(track_arrays(tracks_by_id[tb])["cy"])
            if abs(cy_a - cy_b) < 0.1 * H:   # lado a lado: esquerda em cima
                cx_a = np.mean(track_arrays(tracks_by_id[ta])["cx"])
                cx_b = np.mean(track_arrays(tracks_by_id[tb])["cx"])
                order = (ta, tb) if cx_a <= cx_b else (tb, ta)
            else:
                order = (ta, tb) if cy_a <= cy_b else (tb, ta)
            for key, tid in (("crop_top", order[0]), ("crop_bottom", order[1])):
                if tid not in cache:
                    a = track_arrays(tracks_by_id[tid])
                    ch = float(np.clip(np.median(a["h"]) * cfg.zoom_factor, cfg.min_crop_h_ratio * H * 0.6, H))
                    cw = min(W, ch * half_ar)
                    ch = cw / half_ar
                    cx = gaussian_filter1d(a["cx"], fps * 0.4, mode="nearest")
                    cy = gaussian_filter1d(a["cy"], fps * 0.4, mode="nearest")
                    cache[tid] = (a["frames"], cx, cy, cw, ch)
                afr, cx, cy, cw, ch = cache[tid]
                px = float(np.clip(np.interp(f, afr, cx) - cw / 2, 0, W - cw))
                py = float(np.clip(np.interp(f, afr, cy) - ch * 0.5, 0, H - ch))
                fr[key] = [px, py, cw, ch]

    for i in range(n):
        if frames[i] is None:
            cw, ch = crop_size_for(H, W, H, cfg.output_w, cfg.output_h, cfg)
            frames[i] = {"layout": cfg.no_face_mode, "crop": [(W - cw) / 2, (H - ch) / 2, cw, ch],
                         "track": -1, "mode": "nobody", "scene": -1}

    out = {"config": cfg.to_dict(), "fps": fps, "width": W, "height": H, "start_frame": sf, "end_frame": ef,
           "frames": frames, "segments": segments}
    path = cfg.work / f"{cfg.tag}_plan.json"
    path.write_text(json.dumps(out))
    logger.info("v10.plan -> %s (%d segmentos)", path, len(segments))
    for s in segments:
        logger.info("  %.2f-%.2fs  track=%s  %s %s",
                    s["f0"] / fps, s["f1"] / fps, s["track"], s["mode"], s.get("trans", ""))
    return out
