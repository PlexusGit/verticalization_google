from fastapi.testclient import TestClient

from app.main import app
from app.models import VideoStatus
from app.routers import api
from app.routers.api import _approval_block_reason


client = TestClient(app)


def test_approval_is_only_open_during_pending_review():
    assert _approval_block_reason({"status": VideoStatus.TRANSCRIBING.value})
    assert _approval_block_reason({"status": VideoStatus.SUGGESTING.value})
    assert _approval_block_reason({"status": VideoStatus.PENDING_REVIEW.value}) is None


def test_approval_is_locked_once_persisted_or_completed():
    assert _approval_block_reason({"status": VideoStatus.PENDING_REVIEW.value, "approved_at": "now"})
    assert _approval_block_reason({"status": VideoStatus.RENDERING_FINALS.value})
    assert _approval_block_reason({"status": VideoStatus.COMPLETED.value})


def test_approval_applies_selected_template_to_chapter(monkeypatch):
    class Chapter:
        id = "chapter_001"

        def __init__(self):
            self.reference = self
            self.written = None

        def to_dict(self):
            return {"id": self.id, "title": "Economia", "render_options": {}}

        def set(self, data, merge=False):
            self.written = data

    class Chapters:
        def __init__(self, chapter): self.chapter = chapter
        def stream(self): return [self.chapter]

    class VideoReference:
        def __init__(self, chapter): self.chapter = chapter; self.written = None
        def collection(self, _name): return Chapters(self.chapter)
        def update(self, data): self.written = data

    class Video:
        exists = True

        def __init__(self, chapter): self.reference = VideoReference(chapter)
        def to_dict(self): return {"title": "Jornal", "status": VideoStatus.PENDING_REVIEW.value}

    class TemplateSnapshot:
        exists = True
        def to_dict(self): return {"id": "template_1", "title": "Plantão", "title_macro": "{video_title}: {chapter_title}", "title_position": "top-center", "logo": None}

    class TemplateReference:
        def get(self): return TemplateSnapshot()

    class Templates:
        def document(self, _template_id): return TemplateReference()

    class DB:
        def collection(self, _name): return Templates()

    chapter = Chapter()
    video = Video(chapter)
    monkeypatch.setattr(api, "_unauthorized", lambda _request: None)
    monkeypatch.setattr(api, "_video_snapshot", lambda _video_id: video)
    monkeypatch.setattr(api, "get_db", lambda: DB())
    monkeypatch.setattr(api, "render_final_chapters", lambda *_args: None)

    response = client.post(
        "/api/video/v_test/approve",
        json={"chapter_ids": [chapter.id], "template_ids": {chapter.id: "template_1"}},
    )

    assert response.status_code == 202
    assert chapter.written["template_id"] == "template_1"
    assert chapter.written["render_options"]["branding"]["text"]["text"] == "Jornal: Economia"
    assert video.reference.written["approved_templates"] == {chapter.id: "template_1"}
