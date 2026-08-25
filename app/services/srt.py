"""Small dependency-free SRT parser, formatter, and slicer."""
from __future__ import annotations

from dataclasses import dataclass
import re

_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def parse_time(value: str) -> float:
    match = _TIME.match(value.strip())
    if not match:
        raise ValueError(f"invalid SRT time: {value}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def format_time(seconds: float) -> str:
    millis_total = max(0, round(seconds * 1000))
    hours, remainder = divmod(millis_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt(value: str) -> list[Cue]:
    """Parse ordinary SRT blocks; malformed blocks are rejected by callers."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if len(lines) < 2 or "-->" not in lines[0]:
            raise ValueError("invalid SRT block")
        start_text, end_text = (part.strip() for part in lines[0].split("-->", 1))
        start, end = parse_time(start_text), parse_time(end_text)
        text = "\n".join(lines[1:]).strip()
        if not text or end <= start:
            raise ValueError("invalid SRT cue")
        cues.append(Cue(start, end, text))
    return sorted(cues, key=lambda cue: cue.start)


def to_srt(cues: list[Cue]) -> str:
    return "\n\n".join(
        f"{index}\n{format_time(cue.start)} --> {format_time(cue.end)}\n{cue.text}"
        for index, cue in enumerate(cues, start=1)
    )


def slice_srt(value: str, start: float, end: float) -> str:
    """Keep overlapping cues and shift them to chapter-relative time."""

    selected: list[Cue] = []
    for cue in parse_srt(value):
        if cue.end <= start or cue.start >= end:
            continue
        selected.append(Cue(max(cue.start, start) - start, min(cue.end, end) - start, cue.text))
    return to_srt(selected)
