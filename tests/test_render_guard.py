"""Only the latest dispatched render may update a chapter's visible fields.

Scenario: the reviewer changes the chapter bounds while the original draft
render is still in flight.  The newer render owns the chapter fields; when the
older render finishes it must record its output without overwriting them.
"""
import pytest

from app.models import RenderRequest
from app.services import processing
from app.services.processing import render_chapter


class FakeSnapshot:
    def __init__(self, data, exists=True):
        self._data = data
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class FakeRenderRef:
    def __init__(self, store, doc_id):
        self.store = store
        self.id = doc_id

    def set(self, data):
        self.store[self.id] = dict(data)

    def update(self, data):
        self.store.setdefault(self.id, {}).update(data)


class FakeRenders:
    def __init__(self, store):
        self.store = store

    def document(self, doc_id=None):
        return FakeRenderRef(self.store, doc_id or "new")


class FakeChapterRef:
    def __init__(self, data):
        self.data = data
        self.renders = {}

    def get(self):
        return FakeSnapshot(self.data)

    def update(self, data):
        self.data.update(data)

    def collection(self, _name):
        return FakeRenders(self.renders)


class FakeChapters:
    def __init__(self, chapter_ref):
        self.chapter_ref = chapter_ref

    def document(self, _chapter_id):
        return self.chapter_ref


class FakeVideoRef:
    def __init__(self, video_data, chapter_ref):
        self.video_data = video_data
        self.chapter_ref = chapter_ref

    def get(self):
        return FakeSnapshot(self.video_data)

    def collection(self, _name):
        return FakeChapters(self.chapter_ref)


class FakeUuidModule:
    def __init__(self, sequence):
        self.sequence = list(sequence)

    def uuid4(self):
        value = self.sequence.pop(0)
        return type("Uuid", (), {"hex": value})()


@pytest.fixture
def chapter_ref(monkeypatch):
    ref = FakeChapterRef({"start_seconds": 0, "end_seconds": 60, "subtitle_text": "", "title": "Teste"})
    monkeypatch.setattr(processing, "_video_ref", lambda _video_id: FakeVideoRef({"video_resolution": {"w": 640, "h": 360}}, ref))
    monkeypatch.setattr(processing.events, "record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(processing.srt, "parse_srt", lambda _text: [])
    return ref


def test_superseded_render_does_not_overwrite_newer_draft(monkeypatch, chapter_ref):
    sequence = FakeUuidModule(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    monkeypatch.setattr(processing, "uuid", sequence)

    def fake_submit(payload):
        if payload["render_id"] == "draft_aaaaaaaaaa":
            # While the first render is in flight, the reviewer changes the
            # bounds and a newer render is dispatched and completes.
            monkeypatch.setattr(processing, "uuid", FakeUuidModule(["bbbbbbbbbbbb"]))
            render_chapter("v_test", "chapter_001", RenderRequest(), final=False)
            return {"gcs_path": "old-60s.mp4"}
        return {"gcs_path": "new-full-length.mp4"}

    monkeypatch.setattr(processing.vertical_render, "submit", fake_submit)
    monkeypatch.setattr(processing, "uuid", FakeUuidModule(["aaaaaaaaaaaa"]))

    render_chapter("v_test", "chapter_001", RenderRequest(), final=False)

    # The chapter keeps the newest render's output.
    assert chapter_ref.data["draft_gcs_path"] == "new-full-length.mp4"
    assert chapter_ref.data["draft_render_id"] == "draft_bbbbbbbbbb"
    assert chapter_ref.data["draft_status"] == "COMPLETED"
    # Both render documents completed; the superseded one just stays invisible.
    assert chapter_ref.renders["draft_aaaaaaaaaa"]["status"] == "COMPLETED"
    assert chapter_ref.renders["draft_bbbbbbbbbb"]["status"] == "COMPLETED"


def test_latest_render_updates_chapter_fields(monkeypatch, chapter_ref):
    monkeypatch.setattr(processing, "uuid", FakeUuidModule(["cccccccccccc"]))
    monkeypatch.setattr(processing.vertical_render, "submit", lambda _payload: {"gcs_path": "output.mp4"})

    render_chapter("v_test", "chapter_001", RenderRequest(), final=False)

    assert chapter_ref.data["draft_gcs_path"] == "output.mp4"
    assert chapter_ref.data["draft_render_id"] == "draft_cccccccccc"
    assert chapter_ref.data["draft_status"] == "COMPLETED"
    assert chapter_ref.renders["draft_cccccccccc"]["status"] == "COMPLETED"


def test_v10_render_drops_stale_v3_focal_points(monkeypatch, chapter_ref):
    # A chapter previously rendered with the legacy engine keeps v3 focal
    # points in render_options. A v10 render returns an empty point list plus
    # a camera plan; the stale v3 trajectory must not be persisted under
    # the v10 strategy version.
    chapter_ref.data["render_options"] = {
        "layout_mode": "center_crop",
        "automatic_focal_points": [{"time": 0, "x": 100, "y": 50}],
        "focal_strategy_version": 3,
    }
    monkeypatch.setattr(processing, "uuid", FakeUuidModule(["dddddddddddd"]))
    monkeypatch.setattr(
        processing.vertical_render,
        "submit",
        lambda _payload: {
            "gcs_path": "v10-output.mp4",
            "automatic_focal_points": [],
            "focal_strategy_version": 10,
            "camera_plan_gcs_path": "v_test/chapters/chapter_001/autoframe/v10/abc.plan.json.gz",
            "camera_plan_fingerprint": "abc123",
        },
    )

    render_chapter("v_test", "chapter_001", RenderRequest(), final=False)

    options = chapter_ref.data["render_options"]
    assert options["focal_strategy_version"] == 10
    assert options["camera_plan_gcs_path"].endswith("abc.plan.json.gz")
    assert options["camera_plan_fingerprint"] == "abc123"
    assert "automatic_focal_points" not in options
