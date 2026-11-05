import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from google.genai import errors as genai_errors

from app.config import settings
from app.services import transcribe
from app.services.srt import Cue, parse_srt


def word(text, start, end):
    return NS(word=text, start_offset=f"{start}s", end_offset=f"{end}s")


def response(words, text="x"):
    part = NS(audio_transcription=NS(words=words))
    return NS(candidates=[NS(content=NS(parts=[part]))], text=text)


def api_error(code):
    return genai_errors.APIError(code, {"error": {"message": f"boom {code}", "status": "X"}})


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(transcribe, "_sleep", lambda seconds: None)
    monkeypatch.setattr(settings, "TRANSCRIBE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "TRANSCRIBE_CONCURRENCY", 1)


class FakeBlob:
    def __init__(self, name, downloads):
        self.name, self.downloads = name, downloads

    def download_to_filename(self, destination):
        self.downloads.append(self.name)
        Path(destination).write_bytes(b"mp3-bytes")


class FakeBucket:
    def __init__(self):
        self.downloads = []

    def blob(self, name):
        return FakeBlob(name, self.downloads)


def install(monkeypatch, handler):
    """Patch GCS and the Gemini client; ``handler(contents, config)`` answers each call."""
    bucket = FakeBucket()
    calls = []

    class FakeModels:
        def generate_content(self, model, contents, config):
            calls.append({"model": model, "contents": contents, "config": config})
            return handler(contents, config, len(calls))

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append({"client_kwargs": kwargs})
            self.models = FakeModels()

    monkeypatch.setattr(transcribe.storage, "bucket", lambda name=None: bucket)
    monkeypatch.setattr(transcribe.genai, "Client", FakeClient)
    return bucket, calls


def test_parse_offset_variants():
    parse = transcribe.parse_offset
    assert parse("12.34s") == 12.34
    assert parse(3) == 3.0
    assert parse(datetime.timedelta(seconds=2.5)) == 2.5
    assert parse("01:02:03.5") == 3723.5
    assert parse("02:03.5") == 123.5
    assert parse(None) == 0.0
    assert parse(NS(seconds=2, nanos=500_000_000)) == 2.5


def test_extract_words_repairs_missing_end_and_skips_blanks():
    words = transcribe.extract_words(response([word("oi", 1.0, 0.5), word("  ", 2, 3), word("tudo", 2.0, 2.4)]))
    assert words == [("oi", 1.0, 1.3), ("tudo", 2.0, 2.4)]


def test_group_cues_limits_offset_and_order():
    words = [(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(40)]
    cues = transcribe.group_cues(words, offset=600.0)
    assert len(cues) >= 4
    assert cues[0].start == 600.0
    for cue in cues:
        assert cue.end - cue.start <= transcribe.MAX_SECONDS_PER_CUE + 0.5
        assert len(cue.text) <= transcribe.MAX_CHARS_PER_CUE + 10
    assert [cue.start for cue in cues] == sorted(cue.start for cue in cues)


def test_group_cues_splits_on_long_pause_and_sentence_end():
    words = [("Olá", 0.0, 0.3), ("mundo", 0.4, 0.7), ("de", 5.0, 5.2), ("novo.", 5.3, 5.6)]
    assert [cue.text for cue in transcribe.group_cues(words, 0)] == ["Olá mundo", "de novo."]


def test_non_overlapping_trims_ends_and_keeps_srt_parseable():
    cues = transcribe.non_overlapping([Cue(0, 5, "a"), Cue(3, 4, "b"), Cue(3, 3, "c")])
    assert [(cue.start, cue.end) for cue in cues][0][1] <= 3.0
    assert all(cue.end > cue.start for cue in cues)


def test_transcribe_chunks_builds_srt_on_the_video_timeline(monkeypatch):
    def handler(contents, config, call_number):
        return response([word("primeira", 1.0, 1.5), word("fala.", 1.6, 2.0)]) if call_number == 2 else response([word("segunda", 0.5, 1.0), word("fala.", 1.1, 1.5)])

    bucket, calls = install(monkeypatch, handler)
    events = []
    srt_text = transcribe.transcribe_chunks(
        [
            {"path": "v1/audio/chunk_001.mp3", "offset_seconds": 600.026},
            {"path": "gs://" + settings.BUCKET_NAME + "/v1/audio/chunk_000.mp3", "offset_seconds": 0},
        ],
        callback=lambda action, message, details: events.append(action),
    )
    cues = parse_srt(srt_text)
    assert [cue.text for cue in cues] == ["primeira fala.", "segunda fala."]
    assert cues[0].start == pytest.approx(1.0)
    assert cues[1].start == pytest.approx(600.526)
    assert bucket.downloads == ["v1/audio/chunk_000.mp3", "v1/audio/chunk_001.mp3"]
    request = next(call for call in calls if "model" in call)
    assert request["model"] == settings.TRANSCRIBE_MODEL
    transcription = request["config"].audio_transcription_config
    assert transcription.word_timestamp is True
    assert transcription.language_codes == ["pt-BR"]
    assert request["contents"][0].inline_data.mime_type == "audio/mp3"
    assert events[0] == "gemini_started" and events[-1] == "gemini_completed"
    assert "gemini_chunk_parsed" in events


def test_legacy_flac_chunks_keep_their_mime_type(monkeypatch):
    bucket, calls = install(monkeypatch, lambda *_: response([word("a", 0, 1)]))
    transcribe.transcribe_chunks([{"path": "v1/audio/chunk_000.flac", "offset_seconds": 0}])
    request = next(call for call in calls if "model" in call)
    assert request["contents"][0].inline_data.mime_type == "audio/flac"


@pytest.mark.parametrize("code", [429, 500, 503])
def test_transient_api_errors_are_retried_with_events(monkeypatch, code):
    def handler(contents, config, call_number):
        if call_number <= 2:  # call 1 is client construction
            raise api_error(code)
        return response([word("ok", 0, 1)])

    install(monkeypatch, handler)
    actions = []
    srt_text = transcribe.transcribe_chunks(
        [{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}],
        callback=lambda action, message, details: actions.append(action),
    )
    assert "ok" in srt_text
    assert actions.count("gemini_request_retry") == 1


def test_network_errors_are_retryable():
    assert transcribe.is_retryable(httpx.ConnectError("reset"))
    assert transcribe.is_retryable(TimeoutError())
    assert not transcribe.is_retryable(ValueError("bad"))
    assert not transcribe.is_retryable(api_error(400))


def test_permanent_errors_fail_immediately(monkeypatch):
    attempts = []

    def handler(contents, config, call_number):
        attempts.append(call_number)
        raise api_error(400)

    install(monkeypatch, handler)
    with pytest.raises(genai_errors.APIError):
        transcribe.transcribe_chunks([{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}])
    assert len(attempts) == 1


def test_exhausted_retries_raise_so_the_srt_never_has_holes(monkeypatch):
    install(monkeypatch, lambda *_: (_ for _ in ()).throw(api_error(503)))
    actions = []
    with pytest.raises(RuntimeError, match="após 3 tentativas"):
        transcribe.transcribe_chunks(
            [{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}],
            callback=lambda action, message, details: actions.append(action),
        )
    assert actions[-1] == "gemini_request_failed"


def test_text_without_word_timestamps_is_retried_then_fails(monkeypatch):
    install(monkeypatch, lambda *_: NS(candidates=[], text="só texto"))
    with pytest.raises(RuntimeError, match="tempos por palavra"):
        transcribe.transcribe_chunks([{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}])


def test_silence_in_every_chunk_is_an_error(monkeypatch):
    install(monkeypatch, lambda *_: NS(candidates=[], text=""))
    with pytest.raises(RuntimeError, match="nenhuma fala"):
        transcribe.transcribe_chunks([{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}])


def test_silent_chunk_does_not_break_other_chunks(monkeypatch):
    def handler(contents, config, call_number):
        return NS(candidates=[], text="") if call_number == 2 else response([word("fala", 0, 1)])

    install(monkeypatch, handler)
    srt_text = transcribe.transcribe_chunks(
        [
            {"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0},
            {"path": "v1/audio/chunk_001.mp3", "offset_seconds": 600},
        ]
    )
    assert [cue.text for cue in parse_srt(srt_text)] == ["fala"]


def test_requires_chunks_and_api_key(monkeypatch):
    with pytest.raises(ValueError):
        transcribe.transcribe_chunks([])
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        transcribe.transcribe_chunks([{"path": "x.mp3", "offset_seconds": 0}])


def test_language_codes_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "TRANSCRIBE_LANGUAGE_CODES", " pt-BR , en-US ,")
    assert transcribe._language_codes() == ["pt-BR", "en-US"]
    monkeypatch.setattr(settings, "TRANSCRIBE_LANGUAGE_CODES", "")
    assert transcribe._language_codes() == []


def test_temporary_audio_is_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe.tempfile, "tempdir", str(tmp_path))
    install(monkeypatch, lambda *_: response([word("a", 0, 1)]))
    transcribe.transcribe_chunks([{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}])
    assert list(tmp_path.iterdir()) == []
