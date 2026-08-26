"""Pure rendering and validation primitives for the vertical render worker.

This module intentionally has no Google Cloud dependencies.  Keeping ffmpeg command
construction isolated makes it straightforward to unit test and to replace the
rendering backend without changing the HTTP/GCS orchestration layer.
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
TRANSITION_SECONDS = 2.0
MAX_CAPTIONS = 2_000
MAX_KEYFRAMES = 10_000
MAX_EXPRESSION_KEYFRAMES = 24
_COLOR_RE = re.compile(r"^(?:#[0-9a-fA-F]{6}|[a-zA-Z]{1,24})$")


class ValidationError(ValueError):
    """Raised when a render request is unsafe or structurally invalid."""


@dataclasses.dataclass(frozen=True)
class RenderSpec:
    source_gcs_path: str
    output_gcs_path: str
    mode: str
    trim_start: float
    trim_end: float
    source_width: int
    source_height: int
    keyframes_top: tuple[dict[str, float], ...] = ()
    keyframes_bottom: tuple[dict[str, float], ...] = ()
    automatic_focal_points: tuple[dict[str, float], ...] = ()
    captions: tuple[dict[str, Any], ...] = ()
    caption_style: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    logo: Mapping[str, Any] | None = None
    text_overlays: tuple[Mapping[str, Any], ...] = ()

    @property
    def duration(self) -> float:
        return self.trim_end - self.trim_start


def _number(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValidationError(f"{name} is outside the allowed range")
    return result


def _gcs_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    value = value.strip()
    if "\x00" in value or value.startswith(("/", "file:")) or ".." in value.split("/"):
        raise ValidationError(f"{name} is not a safe GCS path")
    return value


def _keyframes(raw: Any, name: str, maximum: int = MAX_KEYFRAMES) -> tuple[dict[str, float], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > maximum:
        raise ValidationError(f"{name} must be a list with at most {maximum} entries")
    result = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"{name}[{index}] must be an object")
        result.append({
            "time": _number(item.get("time"), f"{name}[{index}].time", 0),
            "x": _number(item.get("x", 0), f"{name}[{index}].x"),
            "y": _number(item.get("y", 0), f"{name}[{index}].y"),
        })
    return tuple(sorted(result, key=lambda frame: frame["time"]))


def validate_request(payload: Any) -> RenderSpec:
    """Validate and normalize an untrusted HTTP JSON payload."""
    if not isinstance(payload, dict):
        raise ValidationError("JSON body must be an object")
    mode = payload.get("mode", "center_crop")
    if mode not in {"center_crop", "split_stack"}:
        raise ValidationError("mode must be center_crop or split_stack")

    resolution = payload.get("video_resolution")
    if not isinstance(resolution, dict):
        raise ValidationError("video_resolution is required")
    width = int(_number(resolution.get("w"), "video_resolution.w", 16))
    height = int(_number(resolution.get("h"), "video_resolution.h", 16))
    start = _number(payload.get("trim_start", 0), "trim_start", 0)
    end = _number(payload.get("trim_end"), "trim_end", 0)
    if end <= start:
        raise ValidationError("trim_end must be greater than trim_start")
    if end - start > 6 * 60 * 60:
        raise ValidationError("render duration cannot exceed 6 hours")

    raw_captions = payload.get("captions", [])
    if not isinstance(raw_captions, list) or len(raw_captions) > MAX_CAPTIONS:
        raise ValidationError(f"captions must be a list with at most {MAX_CAPTIONS} entries")
    captions = []
    for index, caption in enumerate(raw_captions):
        if not isinstance(caption, dict) or not isinstance(caption.get("text"), str):
            raise ValidationError(f"captions[{index}] must contain text")
        cap_start = _number(caption.get("start"), f"captions[{index}].start", 0)
        cap_end = _number(caption.get("end"), f"captions[{index}].end", 0)
        if cap_end <= cap_start or len(caption["text"]) > 2_000:
            raise ValidationError(f"captions[{index}] has invalid timing or text")
        captions.append({"start": cap_start, "end": cap_end, "text": caption["text"]})

    style = payload.get("caption_style", {})
    if not isinstance(style, dict):
        raise ValidationError("caption_style must be an object")
    logo = payload.get("logo")
    if logo is not None:
        if not isinstance(logo, dict):
            raise ValidationError("logo must be an object")
        _gcs_path(logo.get("gcs_path"), "logo.gcs_path")
    overlays = payload.get("text_overlays", [])
    if not isinstance(overlays, list) or len(overlays) > 20:
        raise ValidationError("text_overlays must be a list with at most 20 entries")
    for index, overlay in enumerate(overlays):
        if not isinstance(overlay, dict) or not isinstance(overlay.get("text"), str):
            raise ValidationError(f"text_overlays[{index}] must contain text")
        if len(overlay["text"]) > 500:
            raise ValidationError(f"text_overlays[{index}].text is too long")

    return RenderSpec(
        source_gcs_path=_gcs_path(payload.get("source_gcs_path"), "source_gcs_path"),
        output_gcs_path=_gcs_path(payload.get("output_gcs_path") or payload.get("target_gcs_path"), "output_gcs_path"),
        mode=mode,
        trim_start=start,
        trim_end=end,
        source_width=width,
        source_height=height,
        keyframes_top=_keyframes(payload.get("keyframes_top"), "keyframes_top"),
        keyframes_bottom=_keyframes(payload.get("keyframes_bottom"), "keyframes_bottom"),
        automatic_focal_points=_keyframes(payload.get("automatic_focal_points"), "automatic_focal_points", 241),
        captions=tuple(captions),
        caption_style=style,
        logo=logo,
        text_overlays=tuple(overlays),
    )


def select_automatic_focal_point(
    time_seconds: float,
    width: int,
    height: int,
    focal_points: Sequence[Mapping[str, float]] = (),
) -> tuple[float, float]:
    """Retorna o foco facial interpolado ou o centro determinístico."""
    return (
        eased_value(time_seconds, focal_points, "x", width / 2.0),
        eased_value(time_seconds, focal_points, "y", height / 2.0),
    )


def eased_value(time_seconds: float, frames: Sequence[Mapping[str, float]], field: str, default: float) -> float:
    """RPP-compatible cosine interpolation, useful for previews and tests."""
    if not frames:
        return default
    ordered = sorted(frames, key=lambda frame: frame["time"])
    if time_seconds <= ordered[0]["time"]:
        return float(ordered[0][field])
    if time_seconds >= ordered[-1]["time"]:
        return float(ordered[-1][field])
    for left, right in zip(ordered, ordered[1:]):
        if left["time"] <= time_seconds <= right["time"]:
            gap = right["time"] - left["time"]
            transition = min(TRANSITION_SECONDS, gap)
            move_start = right["time"] - transition
            if time_seconds <= move_start:
                return float(left[field])
            progress = (time_seconds - move_start) / transition
            eased = 0.5 * (1 - math.cos(math.pi * progress))
            return float(left[field] + (right[field] - left[field]) * eased)
    return default


def compact_keyframes(
    frames: Sequence[Mapping[str, float]],
    maximum: int = MAX_EXPRESSION_KEYFRAMES,
) -> tuple[dict[str, float], ...]:
    """Preserva as maiores mudanças da trajetória dentro do limite do parser FFmpeg."""
    compacted = [dict(frame) for frame in sorted(frames, key=lambda frame: frame["time"])]
    while len(compacted) > maximum:
        removable_index, smallest_error = 1, math.inf
        for index in range(1, len(compacted) - 1):
            left, current, right = compacted[index - 1], compacted[index], compacted[index + 1]
            time_span = right["time"] - left["time"]
            if time_span <= 0:
                error = 0.0
            else:
                progress = (current["time"] - left["time"]) / time_span
                expected_x = left["x"] + (right["x"] - left["x"]) * progress
                expected_y = left["y"] + (right["y"] - left["y"]) * progress
                error = math.hypot(current["x"] - expected_x, current["y"] - expected_y)
            if error < smallest_error:
                removable_index, smallest_error = index, error
        compacted.pop(removable_index)
    return tuple(compacted)


def keyframe_expression(trim_start: float, duration: float, frames: Sequence[Mapping[str, float]], field: str, default: float) -> str:
    """Build an ffmpeg expression with RPP easing and absolute-time keyframes."""
    start = eased_value(trim_start, frames, field, default)
    relevant = [(frame["time"] - trim_start, float(frame[field])) for frame in frames if trim_start < frame["time"] <= trim_start + duration]
    expression, current, previous = "", start, 0.0
    closings = 0
    for target_time, target in relevant:
        gap = target_time - previous
        transition = min(TRANSITION_SECONDS, gap) if gap > 0 else 0.001
        move_start = target_time - transition
        expression += f"if(lte(t,{move_start:g}),{current:g},if(lte(t,{target_time:g}),{current:g}+({target:g}-{current:g})*0.5*(1-cos(PI*(t-{move_start:g})/{transition:g})),"
        closings += 2
        current, previous = target, target_time
    return expression + f"{current:g}" + ")" * closings


def _escape_filter_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("'", r"\'").replace(":", r"\:").replace("%", r"\%").replace("\n", r"\n")


def _color(value: Any, default: str) -> str:
    candidate = str(value or default)
    if not _COLOR_RE.fullmatch(candidate):
        raise ValidationError(f"invalid color: {candidate}")
    return candidate


def _position(position: Any, width_expr: str = "w", height_expr: str = "h") -> tuple[str, str]:
    named = {
        "top_left": ("40", "40"), "top_center": (f"({width_expr}-overlay_w)/2", "40"),
        "top_right": (f"{width_expr}-overlay_w-40", "40"), "center": (f"({width_expr}-overlay_w)/2", f"({height_expr}-overlay_h)/2"),
        "bottom_left": ("40", f"{height_expr}-overlay_h-40"), "bottom_center": (f"({width_expr}-overlay_w)/2", f"{height_expr}-overlay_h-40"),
        "bottom_right": (f"{width_expr}-overlay_w-40", f"{height_expr}-overlay_h-40"),
    }
    return named.get(position, named["bottom_right"])


def build_video_filter(spec: RenderSpec, ass_path: str | None, logo_input_index: int | None) -> tuple[str, str]:
    """Return (filter_complex, final_label) for a validated render."""
    if spec.mode == "center_crop":
        # Crop in source coordinates first so the focal-strategy result is an
        # actual extension point rather than a no-op hidden behind scale/crop.
        target_aspect = OUTPUT_WIDTH / OUTPUT_HEIGHT
        source_aspect = spec.source_width / spec.source_height
        if source_aspect >= target_aspect:
            crop_height = spec.source_height - spec.source_height % 2
            crop_width = int(crop_height * target_aspect) // 2 * 2
        else:
            crop_width = spec.source_width - spec.source_width % 2
            crop_height = int(crop_width / target_aspect) // 2 * 2
        focal_points = compact_keyframes(spec.automatic_focal_points)
        focal_x, focal_y = select_automatic_focal_point(spec.trim_start, spec.source_width, spec.source_height, focal_points)
        if focal_points:
            x = keyframe_expression(spec.trim_start, spec.duration, focal_points, "x", focal_x)
            y = keyframe_expression(spec.trim_start, spec.duration, focal_points, "y", focal_y)
            safe_x = f"max(0,min({spec.source_width-crop_width},({x})-{crop_width/2:g}))"
            safe_y = f"max(0,min({spec.source_height-crop_height},({y})-{crop_height/2:g}))"
            filters = [f"[0:v]crop={crop_width}:{crop_height}:'{safe_x}':'{safe_y}',scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}[v0]"]
        else:
            crop_x = max(0, min(spec.source_width - crop_width, focal_x - crop_width / 2))
            crop_y = max(0, min(spec.source_height - crop_height, focal_y - crop_height / 2))
            filters = [f"[0:v]crop={crop_width}:{crop_height}:{crop_x:g}:{crop_y:g},scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}[v0]"]
    else:
        crop_w = min(spec.source_width, int(spec.source_height * 9 / 16))
        crop_w -= crop_w % 2
        top_h = (spec.source_height // 2) // 2 * 2
        bottom_h = (spec.source_height - top_h) // 2 * 2
        default_x = max(0.0, (spec.source_width - crop_w) / 2)
        parts = []
        for name, frames, crop_h, default_y in (("top", spec.keyframes_top, top_h, 0.0), ("bottom", spec.keyframes_bottom, bottom_h, float(top_h))):
            x = keyframe_expression(spec.trim_start, spec.duration, frames, "x", default_x)
            y = keyframe_expression(spec.trim_start, spec.duration, frames, "y", default_y)
            safe_x = f"max(0,min({spec.source_width-crop_w},{x}))"
            safe_y = f"max(0,min({spec.source_height-crop_h},{y}))"
            parts.append(f"[0:v]crop={crop_w}:{crop_h}:'{safe_x}':'{safe_y}',scale={OUTPUT_WIDTH}:960[v{name}]")
        filters = [*parts, "[vtop][vbottom]vstack=inputs=2[v0]"]

    current = "v0"
    if ass_path:
        escaped_path = ass_path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        filters.append(f"[{current}]subtitles='{escaped_path}'[vcap]")
        current = "vcap"
    if logo_input_index is not None and spec.logo:
        width = int(_number(spec.logo.get("width") or spec.logo.get("size", 180), "logo.width", 1))
        raw_height = spec.logo.get("height")
        height = int(_number(raw_height, "logo.height", 1)) if raw_height is not None else -1
        opacity = min(1.0, _number(spec.logo.get("opacity", 1), "logo.opacity", 0))
        if spec.logo.get("x") is not None and spec.logo.get("y") is not None:
            x = f"{int(_number(spec.logo['x'], 'logo.x', 0))}"
            y = f"{int(_number(spec.logo['y'], 'logo.y', 0))}"
        else:
            x, y = _position(spec.logo.get("position"))
        filters.extend([f"[{logo_input_index}:v]scale={width}:{height},format=rgba,colorchannelmixer=aa={opacity:g}[logo]", f"[{current}][logo]overlay={x}:{y}[vlogo]"])
        current = "vlogo"
    for index, overlay in enumerate(spec.text_overlays):
        text = _escape_filter_text(overlay["text"])
        size = int(_number(overlay.get("size", 52), f"text_overlays[{index}].size", 1))
        opacity = min(1.0, _number(overlay.get("opacity", 1), f"text_overlays[{index}].opacity", 0))
        color = _color(overlay.get("color"), "white")
        # drawtext uses text dimensions rather than overlay dimensions.
        pos = overlay.get("position", "top_left")
        text_positions = {"top_left": ("40", "40"), "top_center": ("(w-text_w)/2", "40"), "top_right": ("w-text_w-40", "40"), "center": ("(w-text_w)/2", "(h-text_h)/2"), "bottom_left": ("40", "h-text_h-40"), "bottom_center": ("(w-text_w)/2", "h-text_h-40"), "bottom_right": ("w-text_w-40", "h-text_h-40")}
        if overlay.get("x") is not None and overlay.get("y") is not None:
            x = f"{int(_number(overlay['x'], f'text_overlays[{index}].x', 0))}"
            y = f"{int(_number(overlay['y'], f'text_overlays[{index}].y', 0))}"
        else:
            x, y = text_positions.get(pos, text_positions["top_left"])
        label = f"vtext{index}"
        filters.append(f"[{current}]drawtext=text='{text}':fontsize={size}:fontcolor={color}@{opacity:g}:x={x}:y={y}[{label}]")
        current = label
    return ";".join(filters), current


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, remainder = divmod(remainder, 60)
    return f"{int(hours)}:{int(minutes):02d}:{remainder:05.2f}"


def write_ass_captions(spec: RenderSpec, path: str) -> None:
    """Write editable static captions as ASS events for ffmpeg/libass burning."""
    style = spec.caption_style
    font = str(style.get("font", "DejaVu Sans")).replace(",", "")[:100]
    size = int(_number(style.get("size", 64), "caption_style.size", 8))
    color = _color(style.get("color"), "#FFFFFF").lstrip("#")
    outline_color = _color(style.get("outline_color"), "#000000").lstrip("#")
    ass_color = f"&H00{color[4:6]}{color[2:4]}{color[0:2]}" if len(color) == 6 else "&H00FFFFFF"
    ass_outline = f"&H00{outline_color[4:6]}{outline_color[2:4]}{outline_color[0:2]}" if len(outline_color) == 6 else "&H00000000"
    outline_width = int(_number(style.get("outline_width", 3), "caption_style.outline_width", 0))
    position = style.get("position", "bottom")
    if position not in {"top", "middle", "bottom"}:
        raise ValidationError("caption_style.position must be top, middle, or bottom")
    alignment = {"top": 8, "middle": 5, "bottom": 2}[position]
    margin_v = int(_number(style.get("margin_bottom", 170), "caption_style.margin_bottom", 0))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUTPUT_WIDTH}", f"PlayResY: {OUTPUT_HEIGHT}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{size},{ass_color},&H000000FF,{ass_outline},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_width},1,{alignment},50,50,{margin_v},1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for caption in spec.captions:
        start = caption["start"] - spec.trim_start
        end = caption["end"] - spec.trim_start
        if end <= 0 or start >= spec.duration:
            continue
        text = caption["text"].replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(min(end, spec.duration))},Default,,0,0,0,,{text}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def render(spec: RenderSpec, input_path: str, output_path: str, ass_path: str | None = None, logo_path: str | None = None, timeout: int = 3500) -> None:
    """Run ffmpeg with an argument array; raises a concise error on failure."""
    filter_graph, final_label = build_video_filter(spec, ass_path, 1 if logo_path else None)
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-ss", f"{spec.trim_start:g}", "-t", f"{spec.duration:g}", "-i", input_path]
    if logo_path:
        command.extend(["-loop", "1", "-i", logo_path])
    command.extend(["-filter_complex", filter_graph, "-map", f"[{final_label}]", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", output_path])
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg render timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "unknown ffmpeg error")[-1500:]
        raise RuntimeError(f"ffmpeg render failed: {detail}") from exc
