"""Manually added chapters must get a draft render like suggested ones."""
from fastapi.testclient import TestClient

from app.main import app
from app.routers import api


client = TestClient(app)


class ChapterDocument:
    exists = True

    def __init__(self, doc_id=None):
        self.id = doc_id
        self.data = {}

    def set(self, data, merge=False):
        if merge:
            self.data.update(data)
        else:
            self.data = data

    def update(self, data):
        self.data.update(data)

    def get(self):
        snapshot = ChapterDocument(self.id)
        snapshot.data = dict(self.data)
        return snapshot

    def to_dict(self):
        return dict(self.data)

    def collection(self, _name):
        return RendersCollection()

    def delete(self):
        return None


class RendersCollection:
    def document(self, _render_id=None):
        return ChapterDocument()

    def stream(self):
        return []


class ChaptersCollection:
    def __init__(self):
        self.new_id = "abc123def456"
        self.documents = {}

    def document(self, doc_id=None):
        if doc_id is None:
            return ChapterDocument(self.new_id)
        if doc_id not in self.documents:
            self.documents[doc_id] = ChapterDocument(doc_id)
        return self.documents[doc_id]

    def stream(self):
        return []


class VideoSnapshot:
    exists = True

    def __init__(self, data):
        self._data = data
        self.chapters = ChaptersCollection()
        chapters = self.chapters
        self.reference = type("Ref", (), {"collection": lambda self, _name: chapters})()

    def to_dict(self):
        return self._data


def _video_data(**overrides):
    data = {
        "status": "PENDING_REVIEW",
        "duration_seconds": 600,
        "subtitle_text": "",
        "gcs_high_path": "v_test/source_high.mp4",
    }
    data.update(overrides)
    return data


def _patch(monkeypatch, video_data):
    snapshot = VideoSnapshot(video_data)
    monkeypatch.setattr(api, "_unauthorized", lambda _request: None)
    monkeypatch.setattr(api, "_video_snapshot", lambda _video_id: snapshot)
    dispatched = []
    monkeypatch.setattr(api, "render_chapter", lambda *args, **kwargs: dispatched.append(args))
    return snapshot, dispatched


def _post_chapter(monkeypatch, video_data):
    snapshot, dispatched = _patch(monkeypatch, video_data)
    response = client.post(
        "/api/video/v_test/chapters",
        json={"title": "Novo capítulo", "reason": "", "start_seconds": 10, "end_seconds": 60, "subtitle_text": "", "selected": True},
    )
    return response, dispatched, snapshot


def test_added_chapter_dispatches_draft_render(monkeypatch):
    response, dispatched, _ = _post_chapter(monkeypatch, _video_data())
    assert response.status_code == 201
    assert len(dispatched) == 1
    video_id, chapter_id, options = dispatched[0]
    assert video_id == "v_test"
    assert chapter_id.startswith("chapter_")
    assert options.final is False


def test_added_chapter_skips_render_while_pipeline_is_busy(monkeypatch):
    response, dispatched, _ = _post_chapter(monkeypatch, _video_data(status="TRANSCRIBING"))
    assert response.status_code == 201
    assert dispatched == []


def test_added_chapter_skips_render_without_source(monkeypatch):
    response, dispatched, _ = _post_chapter(monkeypatch, _video_data(gcs_high_path=None, gcs_source_path=None))
    assert response.status_code == 201
    assert dispatched == []


def test_saving_new_bounds_redispatches_draft(monkeypatch):
    snapshot, dispatched = _patch(monkeypatch, _video_data(duration_seconds=700))
    chapter_id = "chapter_abc"
    snapshot.chapters.document(chapter_id).data = {"start_seconds": 0, "end_seconds": 60, "title": "Novo capítulo"}
    response = client.put(
        f"/api/video/v_test/chapters/{chapter_id}",
        json={"title": "Novo capítulo", "reason": "", "start_seconds": 0, "end_seconds": 612, "subtitle_text": "", "selected": True},
    )
    assert response.status_code == 200
    assert len(dispatched) == 1
    assert dispatched[0][1] == chapter_id
    assert snapshot.chapters.document(chapter_id).data["end_seconds"] == 612


def test_saving_without_bound_changes_does_not_redispatch(monkeypatch):
    snapshot, dispatched = _patch(monkeypatch, _video_data(duration_seconds=700))
    chapter_id = "chapter_abc"
    snapshot.chapters.document(chapter_id).data = {"start_seconds": 0, "end_seconds": 612, "title": "Novo capítulo"}
    response = client.put(
        f"/api/video/v_test/chapters/{chapter_id}",
        json={"title": "Título editado", "reason": "", "start_seconds": 0, "end_seconds": 612, "subtitle_text": "", "selected": True},
    )
    assert response.status_code == 200
    assert dispatched == []


def test_saving_bounds_skips_redispatch_when_pipeline_is_busy(monkeypatch):
    snapshot, dispatched = _patch(monkeypatch, _video_data(status="RENDERING_DRAFTS", duration_seconds=700))
    chapter_id = "chapter_abc"
    snapshot.chapters.document(chapter_id).data = {"start_seconds": 0, "end_seconds": 60, "title": "Novo capítulo"}
    response = client.put(
        f"/api/video/v_test/chapters/{chapter_id}",
        json={"title": "Novo capítulo", "reason": "", "start_seconds": 0, "end_seconds": 612, "subtitle_text": "", "selected": True},
    )
    assert response.status_code == 200
    assert dispatched == []


def test_editor_options_persist_without_render(monkeypatch):
    snapshot, dispatched = _patch(monkeypatch, _video_data(duration_seconds=700))
    chapter_id = "chapter_abc"
    snapshot.chapters.document(chapter_id).data = {"start_seconds": 0, "end_seconds": 612, "title": "Novo capítulo"}
    response = client.put(
        f"/api/video/v_test/chapters/{chapter_id}",
        json={
            "title": "Novo capítulo",
            "start_seconds": 0,
            "end_seconds": 612,
            "subtitle_text": "",
            "template_id": "none",
            "render_options": {
                "layout_mode": "split_stack",
                "keyframes_top": [{"time": 5.0, "x": 100, "y": 50}],
                "caption_style": {"font_size": 72, "position": "top"},
            },
        },
    )
    assert response.status_code == 200
    assert dispatched == []
    stored = snapshot.chapters.document(chapter_id).data["render_options"]
    assert stored["layout_mode"] == "split_stack"
    assert stored["keyframes_top"] == [{"time": 5.0, "x": 100.0, "y": 50.0}]
    assert stored["caption_style"]["font_size"] == 72
    assert stored["caption_style"]["position"] == "top"
    # The raw template choice lives at chapter level, not inside render_options.
    assert stored["template_id"] is None
    assert snapshot.chapters.document(chapter_id).data["template_id"] == "none"


def test_editor_options_preserve_stored_branding_and_plan(monkeypatch):
    snapshot, _ = _patch(monkeypatch, _video_data(duration_seconds=700))
    chapter_id = "chapter_abc"
    snapshot.chapters.document(chapter_id).data = {
        "start_seconds": 0,
        "end_seconds": 612,
        "title": "Novo capítulo",
        "render_options": {
            "branding": {"text": {"text": "approved"}},
            "camera_plan_gcs_path": "v_test/plan.json.gz",
            "camera_plan_fingerprint": "abc",
            "focal_strategy_version": 10,
        },
    }
    response = client.put(
        f"/api/video/v_test/chapters/{chapter_id}",
        json={
            "title": "Novo capítulo",
            "start_seconds": 0,
            "end_seconds": 612,
            "subtitle_text": "",
            "render_options": {"layout_mode": "center_crop"},
        },
    )
    assert response.status_code == 200
    stored = snapshot.chapters.document(chapter_id).data["render_options"]
    assert stored["branding"]["text"]["text"] == "approved"
    assert stored["camera_plan_gcs_path"] == "v_test/plan.json.gz"
    assert stored["camera_plan_fingerprint"] == "abc"
    assert stored["focal_strategy_version"] == 10


def test_bounds_change_rerender_keeps_stored_options(monkeypatch):
    snapshot, dispatched = _patch(monkeypatch, _video_data(duration_seconds=700))
    chapter_id = "chapter_abc"
    snapshot.chapters.document(chapter_id).data = {
        "start_seconds": 0,
        "end_seconds": 60,
        "title": "Novo capítulo",
        "render_options": {
            "layout_mode": "split_stack",
            "keyframes_top": [{"time": 5.0, "x": 100.0, "y": 50.0}],
        },
    }
    response = client.put(
        f"/api/video/v_test/chapters/{chapter_id}",
        json={"title": "Novo capítulo", "start_seconds": 0, "end_seconds": 612, "subtitle_text": ""},
    )
    assert response.status_code == 200
    assert len(dispatched) == 1
    options = dispatched[0][2]
    assert options.layout_mode == "split_stack"
    assert options.keyframes_top[0].x == 100.0
