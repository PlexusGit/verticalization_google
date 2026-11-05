import pytest

from app.config import settings
from app.services import subtitles

CHUNKS = [{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}]


@pytest.fixture
def engines(monkeypatch):
    calls = []

    def make(name, result):
        def engine(chunks, callback):
            calls.append(name)
            if isinstance(result, Exception):
                raise result
            return result

        return engine

    def install(gemini, chirp):
        monkeypatch.setattr(subtitles, "_gemini", make("gemini", gemini))
        monkeypatch.setattr(subtitles, "_chirp", make("chirp", chirp))
        return calls

    monkeypatch.setattr(settings, "SUBTITLE_ENGINE", "gemini")
    monkeypatch.setattr(settings, "SUBTITLE_FALLBACK_TO_CHIRP", True)
    return install


def test_gemini_is_the_default_engine(engines):
    calls = engines("gemini srt", "chirp srt")
    assert subtitles.transcribe_audio_chunks(CHUNKS) == ("gemini srt", subtitles.GEMINI_SOURCE)
    assert calls == ["gemini"]


def test_chirp_engine_skips_gemini(engines, monkeypatch):
    calls = engines("gemini srt", "chirp srt")
    monkeypatch.setattr(settings, "SUBTITLE_ENGINE", " Chirp ")
    assert subtitles.transcribe_audio_chunks(CHUNKS) == ("chirp srt", subtitles.CHIRP_SOURCE)
    assert calls == ["chirp"]


def test_unknown_engine_value_uses_gemini(engines, monkeypatch):
    engines("gemini srt", "chirp srt")
    monkeypatch.setattr(settings, "SUBTITLE_ENGINE", "whisper")
    assert subtitles.configured_engine() == "gemini"


def test_gemini_failure_falls_back_to_chirp_and_reports_it(engines):
    calls = engines(RuntimeError("quota"), "chirp srt")
    events = []
    result = subtitles.transcribe_audio_chunks(CHUNKS, lambda action, message, details: events.append((action, details)))
    assert result == ("chirp srt", subtitles.CHIRP_SOURCE)
    assert calls == ["gemini", "chirp"]
    assert events[0][0] == "engine_fallback"
    assert events[0][1]["error_message"] == "quota"


def test_fallback_can_be_disabled(engines, monkeypatch):
    calls = engines(RuntimeError("quota"), "chirp srt")
    monkeypatch.setattr(settings, "SUBTITLE_FALLBACK_TO_CHIRP", False)
    with pytest.raises(RuntimeError, match="quota"):
        subtitles.transcribe_audio_chunks(CHUNKS)
    assert calls == ["gemini"]


def test_both_engines_failing_reports_both_errors(engines):
    engines(RuntimeError("gemini down"), ValueError("chirp down"))
    with pytest.raises(RuntimeError) as error:
        subtitles.transcribe_audio_chunks(CHUNKS)
    assert "gemini down" in str(error.value) and "chirp down" in str(error.value)
