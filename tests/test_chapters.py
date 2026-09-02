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
