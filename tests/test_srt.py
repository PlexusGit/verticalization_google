import pytest

from app.services.srt import Cue, format_time, parse_srt, parse_time, slice_srt, to_srt


SAMPLE = """1
00:00:10,000 --> 00:00:12,500
Primeira legenda

2
00:00:13,000 --> 00:00:16,000
Segunda legenda
"""


def test_parse_and_format_round_trip():
    cues = parse_srt(SAMPLE)
    assert cues == [Cue(10, 12.5, "Primeira legenda"), Cue(13, 16, "Segunda legenda")]
    assert parse_srt(to_srt(cues)) == cues
    assert format_time(parse_time("01:02:03,456")) == "01:02:03,456"


def test_slice_clamps_and_shifts_to_chapter_time():
    cues = parse_srt(slice_srt(SAMPLE, 11, 14))
    assert cues[0] == Cue(0, 1.5, "Primeira legenda")
    assert cues[1] == Cue(2, 3, "Segunda legenda")


def test_invalid_srt_is_rejected():
    with pytest.raises(ValueError):
        parse_srt("texto sem marcação")
