"""process_video: transcription engine bookkeeping and audio-chunk reuse."""
import pytest

from app.config import settings
from app.services import processing

SRT = "1\n00:00:01,000 --> 00:00:02,000\nolá\n"


class FakeSnapshot:
    def __init__(self, data, exists=True):
        self._data, self.exists = data, exists

    def to_dict(self):
        return dict(self._data)


class FakeChapterRef:
    def __init__(self):
        self.data = {}

    def set(self, data, merge=False):
        self.data.update(data)

    def get(self):
        return FakeSnapshot(self.data)


class FakeVideoRef:
    def __init__(self, data):
        self.data = data
        self.chapters = {}

    def get(self):
        return FakeSnapshot(self.data)

    def update(self, fields):
        self.data.update(fields)

    def collection(self, name):
        assert name == "chapters"
        ref = self

        class Chapters:
            def document(self, chapter_id):
                return ref.chapters.setdefault(chapter_id, FakeChapterRef())

        return Chapters()


@pytest.fixture
def pipeline(monkeypatch):
    state = {"transcode_calls": 0, "transcribe_calls": [], "uploads": []}

    def install(video, *, source="gemini_transcribe"):
        ref = FakeVideoRef({"gcs_source_path": "v1/v1_source.mp4", **video})
        monkeypatch.setattr(processing, "_video_ref", lambda video_id: ref)
        monkeypatch.setattr(processing.storage, "object_exists", lambda path: True)
        monkeypatch.setattr(processing.storage, "upload_bytes", lambda data, path, content_type: state["uploads"].append(path))
        monkeypatch.setattr(processing.events, "record", lambda *args, **kwargs: state.setdefault("events", []).append((args, kwargs)))
        monkeypatch.setattr(processing.gemini, "suggest_chapters", lambda text: [])

        def fake_transcode(video_id, source_path):
            state["transcode_calls"] += 1
            return {
                "high_gcs_path": "v1/v1_high.mp4",
                "gcs_audio_chunks": [{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}],
                "duration_seconds": 60,
                "resolution": {"width": 1920, "height": 1080},
            }

        def fake_transcribe(chunks, callback=None):
            state["transcribe_calls"].append(chunks)
            if callback:
                callback("gemini_started", "iniciada", {})
            return SRT, source

        monkeypatch.setattr(processing.transcode, "submit", fake_transcode)
        monkeypatch.setattr(processing.subtitles, "transcribe_audio_chunks", fake_transcribe)
        return ref

    return install, state


def run(ref):
    processing.process_video("v1")
    return ref.data


def test_transcription_records_the_engine_that_produced_the_srt(pipeline):
    install, state = pipeline
    ref = install({})
    data = run(ref)
    assert data["subtitle_source"] == "gemini_transcribe"
    assert data["subtitle_text"] == SRT
    assert data["gcs_srt_path"] == "v1/v1.srt"
    assert state["transcribe_calls"] == [[{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}]]
    # The suggestion stage runs after transcription (and fails here: no chapters).
    assert data["status"] == "ERROR" and "nenhum capítulo" in data["error"]


def test_chirp_fallback_is_recorded_as_the_source(pipeline):
    install, _ = pipeline
    ref = install({}, source="chirp_3_pt_br")
    assert run(ref)["subtitle_source"] == "chirp_3_pt_br"


def test_mp3_chunks_and_high_rendition_are_reused_on_retry(pipeline):
    install, state = pipeline
    ref = install({"gcs_high_path": "v1/v1_high.mp4", "gcs_audio_chunks": [{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}]})
    run(ref)
    assert state["transcode_calls"] == 0
    assert state["transcribe_calls"]


def test_legacy_flac_chunks_are_regenerated_as_mp3_when_transcription_is_needed(pipeline):
    install, state = pipeline
    ref = install({"gcs_high_path": "v1/v1_high.mp4", "gcs_audio_chunks": [{"path": "v1/audio/chunk_000.flac", "offset_seconds": 0}]})
    run(ref)
    assert state["transcode_calls"] == 1
    assert state["transcribe_calls"][0][0]["path"].endswith(".mp3")


def test_legacy_flac_chunks_are_kept_when_subtitles_already_exist(pipeline):
    install, state = pipeline
    ref = install(
        {
            "gcs_high_path": "v1/v1_high.mp4",
            "gcs_audio_chunks": [{"path": "v1/audio/chunk_000.flac", "offset_seconds": 0}],
            "subtitle_text": SRT,
        }
    )
    run(ref)
    assert state["transcode_calls"] == 0
    assert state["transcribe_calls"] == []


def test_transcription_levels_map_to_event_severity():
    level = processing._transcription_level
    assert level("gemini_request_failed") == "ERROR"
    assert level("chirp_request_failed") == "ERROR"
    assert level("gemini_request_retry") == "WARNING"
    assert level("chirp_resource_exhausted") == "WARNING"
    assert level("engine_fallback") == "WARNING"
    assert level("gemini_chunk_parsed") == "INFO"


def test_started_event_names_the_configured_engine(pipeline, monkeypatch):
    install, state = pipeline
    monkeypatch.setattr(settings, "SUBTITLE_ENGINE", "gemini")
    ref = install({})
    run(ref)
    started = next(kwargs["details"] for args, kwargs in state["events"] if args[2] == "transcription_started")
    assert started["engine"] == "gemini" and started["model"] == settings.TRANSCRIBE_MODEL
