"""An interrupted pipeline resumes without repeating work or losing renders."""
from types import SimpleNamespace

import pytest
import requests

from app.models import RenderRequest
from app.services import lease, processing, vertical_render

SRT = "1\n00:00:01,000 --> 00:00:02,000\nolá\n"


class Snap:
    def __init__(self, data, doc_id="x"):
        self._data, self.exists, self.id = data, data is not None, doc_id
        self.reference = None

    def to_dict(self):
        return dict(self._data or {})


class DocRef:
    def __init__(self, store, doc_id):
        self.store, self.id = store, doc_id

    def get(self):
        snap = Snap(self.store.get(self.id), self.id)
        snap.reference = self
        return snap

    def set(self, data, merge=False):
        self.store.setdefault(self.id, {}).update(data) if merge else self.store.__setitem__(self.id, dict(data))

    def update(self, data):
        self.store.setdefault(self.id, {}).update(data)


class ChapterRef(DocRef):
    def __init__(self, store, doc_id, renders):
        super().__init__(store, doc_id)
        self.renders = renders

    def collection(self, name):
        assert name == "renders"
        renders = self.renders.setdefault(self.id, {})
        return SimpleNamespace(document=lambda render_id: DocRef(renders, render_id))


class Chapters:
    def __init__(self, store, renders):
        self.store, self.renders = store, renders

    def document(self, chapter_id):
        return ChapterRef(self.store, chapter_id, self.renders)

    def stream(self):
        for chapter_id in list(self.store):
            snap = Snap(self.store[chapter_id], chapter_id)
            snap.reference = ChapterRef(self.store, chapter_id, self.renders)
            yield snap


class VideoRef(DocRef):
    def __init__(self, data, chapters):
        super().__init__({"v1": data}, "v1")
        self.chapters, self.renders = chapters, {}

    @property
    def data(self):
        return self.store["v1"]

    def collection(self, name):
        assert name == "chapters"
        return Chapters(self.chapters, self.renders)


@pytest.fixture
def env(monkeypatch):
    """Replays the stuck video: 3 chapters, two of them already rendered by the worker."""

    state = SimpleNamespace(dispatched=[], suggestions=0, events=[], workers={})
    chapters = {
        "chapter_001": {"id": "chapter_001", "start_seconds": 64.1, "end_seconds": 203.9, "subtitle_text": "", "title": "A",
                        "draft_status": "ERROR", "draft_render_id": "draft_aaa", "draft_error": "502 Server Error"},
        "chapter_002": {"id": "chapter_002", "start_seconds": 265.1, "end_seconds": 439.9, "subtitle_text": "", "title": "B",
                        "draft_status": "PROCESSING", "draft_render_id": "draft_bbb"},
        "chapter_003": {"id": "chapter_003", "start_seconds": 500, "end_seconds": 600, "subtitle_text": "", "title": "C",
                        "draft_status": "QUEUED"},
    }
    video = VideoRef(
        {
            "status": "RENDERING_DRAFTS", "gcs_source_path": "v1/src.mp4", "gcs_high_path": "v1/high.mp4",
            "gcs_audio_chunks": [{"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0}],
            "subtitle_text": SRT, "video_resolution": {"w": 1920, "h": 1080},
        },
        chapters,
    )
    state.video, state.chapters = video, chapters
    # The worker finished chapter 1 (old worker: no stored result) and chapter 2 (stored result).
    state.workers = {
        "draft_aaa": {"status": "COMPLETED", "output_gcs_path": "gs://bucket/v1/chapters/chapter_001/draft_aaa.mp4"},
        "draft_bbb": {"status": "COMPLETED", "result": {
            "output_gcs_path": "gs://bucket/v1/chapters/chapter_002/draft_bbb.mp4",
            "focal_strategy_version": 10, "camera_plan_gcs_path": "plan/b.json", "camera_plan_fingerprint": "fp-b"}},
    }
    monkeypatch.setattr(processing, "_video_ref", lambda _id: video)
    monkeypatch.setattr(processing.storage, "object_exists", lambda _p: True)
    monkeypatch.setattr(processing.events, "record", lambda vid, stage, action, *a, **k: state.events.append(action))
    monkeypatch.setattr(processing.srt, "parse_srt", lambda _t: [])
    monkeypatch.setattr(processing.gemini, "suggest_chapters", lambda _t: state.__setattr__("suggestions", state.suggestions + 1) or [])
    monkeypatch.setattr(processing.transcode, "submit", lambda *a: pytest.fail("transcode must be reused"))
    monkeypatch.setattr(processing.subtitles, "transcribe_audio_chunks", lambda *a, **k: pytest.fail("transcription must be reused"))
    monkeypatch.setattr(
        vertical_render, "_render_doc",
        lambda vid, rid: SimpleNamespace(get=lambda: Snap(state.workers.get(rid))),
    )

    def submit(payload):
        state.dispatched.append(payload["render_id"])
        return {"output_gcs_path": f"gs://bucket/{payload['render_id']}.mp4", "focal_strategy_version": 10}

    monkeypatch.setattr(processing.vertical_render, "submit", submit)
    monkeypatch.setattr(vertical_render, "_sleep", lambda _seconds: pytest.fail("a test must never wait on the worker"))
    monkeypatch.setattr(lease, "get_db", lambda: SimpleNamespace(collection=lambda _n: SimpleNamespace(document=lambda _i: video)))
    return state


def test_resume_recovers_the_stuck_video_without_repeating_finished_renders(env):
    processing.process_video("v1", resume=True)

    assert env.suggestions == 0, "stored chapters must be reused, not re-suggested"
    assert len(env.dispatched) == 1, "only the never-dispatched chapter may be rendered"
    c1, c2, c3 = (env.chapters[f"chapter_00{i}"] for i in (1, 2, 3))
    assert c1["draft_status"] == "COMPLETED" and c1["draft_gcs_path"].endswith("draft_aaa.mp4")
    assert c1["draft_render_id"] == "draft_aaa"
    assert c2["draft_status"] == "COMPLETED" and c2["draft_gcs_path"].endswith("draft_bbb.mp4")
    assert c2["render_options"]["camera_plan_gcs_path"] == "plan/b.json"
    assert c3["draft_status"] == "COMPLETED" and c3["draft_render_id"] == env.dispatched[0]
    assert env.video.data["status"] == "PENDING_REVIEW"
    assert "chapters_reused" in env.events and "draft_render_adopted" in env.events


def test_adopting_a_render_without_a_stored_result_forces_the_plan_to_be_recomputed(env):
    processing.process_video("v1", resume=True)
    options = env.chapters["chapter_001"]["render_options"]
    assert options["camera_plan_gcs_path"] == ""
    assert options["focal_strategy_version"] == 0


def test_resume_skips_drafts_that_already_completed(env):
    env.chapters["chapter_001"].update({"draft_status": "COMPLETED", "draft_gcs_path": "done.mp4"})
    processing.process_video("v1", resume=True)
    assert env.chapters["chapter_001"]["draft_gcs_path"] == "done.mp4"
    assert "draft_render_reused" in env.events


def test_a_lost_worker_render_is_dispatched_again(env):
    env.workers["draft_bbb"] = {"status": "ERROR", "error": "worker crashed"}
    processing.process_video("v1", resume=True)
    assert len(env.dispatched) == 2
    assert env.chapters["chapter_002"]["draft_render_id"] in env.dispatched
    assert env.video.data["status"] == "PENDING_REVIEW"


def test_a_missing_worker_record_is_dispatched_again(env):
    del env.workers["draft_aaa"]
    processing.process_video("v1", resume=True)
    assert len(env.dispatched) == 2


def test_without_resume_chapters_are_not_reused(env, monkeypatch):
    monkeypatch.setattr(processing.gemini, "suggest_chapters", lambda _t: [])
    processing.process_video("v1")
    assert env.video.data["status"] == "ERROR"
    assert "nenhum capítulo" in env.video.data["error"]


def test_resume_runs_under_the_heartbeat_lease(env):
    seen = []
    original = processing._process_video
    processing._process_video = lambda vid, resume: seen.append(lease.is_active_here(vid))
    try:
        processing.process_video("v1", resume=True)
    finally:
        processing._process_video = original
    assert seen == [True] and not lease.is_active_here("v1")


def gateway_error(status):
    return requests.HTTPError(response=SimpleNamespace(status_code=status))


def test_a_gateway_error_adopts_the_render_the_worker_finished(env, monkeypatch):
    """The exact failure: the app got 502 after 86 s while the worker went on to succeed."""

    def submit(payload):
        raise gateway_error(502)

    monkeypatch.setattr(processing.vertical_render, "submit", submit)
    env.workers["draft_newnewnewn"] = {"status": "COMPLETED", "result": {"output_gcs_path": "gs://bucket/ok.mp4", "focal_strategy_version": 10}}
    monkeypatch.setattr(processing.uuid, "uuid4", lambda: SimpleNamespace(hex="newnewnewnew"))
    env.chapters["chapter_003"]["draft_status"] = "QUEUED"

    result = processing.render_chapter("v1", "chapter_003", RenderRequest(), final=False)

    assert result["status"] == "COMPLETED"
    assert env.chapters["chapter_003"]["draft_status"] == "COMPLETED"
    assert env.chapters["chapter_003"]["draft_gcs_path"].endswith("ok.mp4")
    assert "render_connection_lost" in env.events


def test_a_gateway_error_with_no_worker_record_still_fails_the_chapter(env, monkeypatch):
    monkeypatch.setattr(processing.vertical_render, "submit", lambda payload: (_ for _ in ()).throw(gateway_error(502)))
    monkeypatch.setattr(
        processing.vertical_render, "await_result",
        lambda *a, **k: (_ for _ in ()).throw(vertical_render.RenderUnavailable("não registrou")),
    )
    with pytest.raises(requests.HTTPError):
        processing.render_chapter("v1", "chapter_003", RenderRequest(), final=False)
    assert env.chapters["chapter_003"]["draft_status"] == "ERROR"


def test_a_real_render_error_is_never_adopted(env, monkeypatch):
    monkeypatch.setattr(processing.vertical_render, "submit", lambda payload: (_ for _ in ()).throw(gateway_error(400)))
    monkeypatch.setattr(processing.vertical_render, "await_result", lambda *a, **k: pytest.fail("must not wait"))
    with pytest.raises(requests.HTTPError):
        processing.render_chapter("v1", "chapter_003", RenderRequest(), final=False)


def test_finals_resume_keeps_completed_finals_and_adopts_running_ones(env):
    env.chapters["chapter_001"].update({"final_status": "COMPLETED", "final_gcs_path": "f1.mp4", "final_render_id": "final_1"})
    env.chapters["chapter_002"].update({"final_status": "PROCESSING", "final_render_id": "final_bbb"})
    env.workers["final_bbb"] = {"status": "COMPLETED", "result": {"output_gcs_path": "gs://bucket/f2.mp4", "focal_strategy_version": 10}}
    env.video.data["status"] = "RENDERING_FINALS"
    env.video.data["approved_chapter_ids"] = ["chapter_001", "chapter_002"]
    for chapter_id in ("chapter_001", "chapter_002"):
        env.chapters[chapter_id]["render_options"] = {}
    processing._render_horizontal_cut = lambda *a, **k: "h.mp4"

    processing.render_final_chapters("v1", ["chapter_001", "chapter_002"], resume=True)

    assert env.dispatched == []
    assert env.chapters["chapter_001"]["final_gcs_path"] == "f1.mp4"
    assert env.chapters["chapter_002"]["final_status"] == "COMPLETED"
    assert env.video.data["status"] == "COMPLETED"


# --- API: a stale video is resumed when someone opens or lists it -----------------

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models import VideoStatus  # noqa: E402
from app.routers import api  # noqa: E402

client = TestClient(app)


class DetailVideo:
    exists = True

    def __init__(self, data):
        self._data = data
        self.reference = SimpleNamespace(collection=lambda _name: SimpleNamespace(stream=lambda: []))

    def to_dict(self):
        return dict(self._data)


def _api_env(monkeypatch, data, claim):
    started = []
    monkeypatch.setattr(api, "_unauthorized", lambda _request: None)
    monkeypatch.setattr(api, "_video_snapshot", lambda _id: DetailVideo(data))
    monkeypatch.setattr(api.lease, "is_stale", lambda *_a, **_k: True)
    monkeypatch.setattr(api.lease, "claim_resume", lambda _id: claim)
    monkeypatch.setattr(api.events, "record", lambda vid, stage, action, *a, **k: started.append(("event", action)))
    monkeypatch.setattr(api, "process_video", lambda vid, resume=False: started.append(("process", vid, resume)))
    monkeypatch.setattr(api, "render_final_chapters", lambda vid, ids, resume=False: started.append(("finals", vid, ids, resume)))
    return started


def test_opening_a_stale_video_resumes_the_pipeline(monkeypatch):
    started = _api_env(monkeypatch, {"status": VideoStatus.RENDERING_DRAFTS.value}, "resume")
    assert client.get("/api/video/v1").status_code == 200
    assert ("process", "v1", True) in started
    assert ("event", "pipeline_resumed") in started


def test_opening_a_stale_finals_batch_resumes_the_approved_chapters(monkeypatch):
    started = _api_env(
        monkeypatch,
        {"status": VideoStatus.RENDERING_FINALS.value, "approved_chapter_ids": ["chapter_001", "chapter_002"]},
        "resume",
    )
    client.get("/api/video/v1")
    assert ("finals", "v1", ["chapter_001", "chapter_002"], True) in started


def test_losing_the_claim_does_not_start_a_second_pipeline(monkeypatch):
    started = _api_env(monkeypatch, {"status": VideoStatus.RENDERING_DRAFTS.value}, "skip")
    client.get("/api/video/v1")
    assert started == []


def test_giving_up_records_an_error_event_and_starts_nothing(monkeypatch):
    started = _api_env(monkeypatch, {"status": VideoStatus.RENDERING_DRAFTS.value}, "give_up")
    client.get("/api/video/v1")
    assert started == [("event", "pipeline_resume_gave_up")]


def test_a_live_pipeline_is_left_alone(monkeypatch):
    started = _api_env(monkeypatch, {"status": VideoStatus.RENDERING_DRAFTS.value}, "resume")
    monkeypatch.setattr(api.lease, "is_stale", lambda *_a, **_k: False)
    client.get("/api/video/v1")
    assert started == []


def test_listing_resumes_at_most_three_stale_videos(monkeypatch):
    started = []
    docs = [SimpleNamespace(id=f"v{i}", to_dict=lambda i=i: {"id": f"v{i}", "status": "RENDERING_DRAFTS"}) for i in range(5)]
    query = SimpleNamespace(order_by=lambda *a, **k: SimpleNamespace(limit=lambda n: SimpleNamespace(stream=lambda: docs)))
    monkeypatch.setattr(api, "_unauthorized", lambda _request: None)
    monkeypatch.setattr(api, "get_db", lambda: SimpleNamespace(collection=lambda _n: query))
    monkeypatch.setattr(api.lease, "is_stale", lambda *_a, **_k: True)
    monkeypatch.setattr(api.lease, "claim_resume", lambda _id: "resume")
    monkeypatch.setattr(api.events, "record", lambda *a, **k: None)
    monkeypatch.setattr(api, "process_video", lambda vid, resume=False: started.append(vid))
    assert client.get("/api/videos").status_code == 200
    assert started == ["v0", "v1", "v2"]
