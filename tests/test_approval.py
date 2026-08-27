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


def test_retry_finals_only_renders_failed_chapters(monkeypatch):
    """retry-finals should skip chapters whose final_status is COMPLETED."""

    class ChapterDoc:
        def __init__(self, chapter_id, final_status):
            self.id = chapter_id
            self._final_status = final_status

        def to_dict(self):
            return {"final_status": self._final_status}

    class ChapterRef:
        def __init__(self, doc): self._doc = doc
        def get(self): return self._doc

    class Chapters:
        def __init__(self, chapters): self._chapters = {c.id: c for c in chapters}
        def document(self, cid): return ChapterRef(self._chapters[cid])

    class VideoRef:
        def __init__(self, chapters): self._chapters = chapters; self.updated = {}
        def collection(self, _name): return Chapters(self._chapters)
        def update(self, data): self.updated = data

    class Video:
        exists = True
        def __init__(self, chapters):
            self.reference = VideoRef(chapters)
        def to_dict(self):
            return {
                "status": VideoStatus.ERROR.value,
                "approved_chapter_ids": ["chapter_001", "chapter_002"],
            }

    chapters = [ChapterDoc("chapter_001", "COMPLETED"), ChapterDoc("chapter_002", "ERROR")]
    video = Video(chapters)
    monkeypatch.setattr(api, "_unauthorized", lambda _request: None)
    monkeypatch.setattr(api, "_video_snapshot", lambda _video_id: video)
    dispatched = []
    monkeypatch.setattr(api, "render_final_chapters", lambda vid, ids: dispatched.append((vid, ids)))

    response = client.post("/api/video/v_test/retry-finals")

    assert response.status_code == 202
    assert response.json()["status"] == VideoStatus.RENDERING_FINALS.value
    assert response.json()["retrying"] == ["chapter_002"]
    assert dispatched == [("v_test", ["chapter_002"])]


def test_retry_finals_completes_when_all_succeeded(monkeypatch):
    """If all approved chapters already have final_status=COMPLETED, mark the video COMPLETED."""

    class ChapterDoc:
        def __init__(self, cid): self.id = cid
        def to_dict(self): return {"final_status": "COMPLETED"}

    class ChapterRef:
        def __init__(self, doc): self._doc = doc
        def get(self): return self._doc

    class Chapters:
        def document(self, cid): return ChapterRef(ChapterDoc(cid))

    class VideoRef:
        def __init__(self): self.updated = {}
        def collection(self, _name): return Chapters()
        def update(self, data): self.updated = data

    class Video:
        exists = True
        def __init__(self): self.reference = VideoRef()
        def to_dict(self):
            return {
                "status": VideoStatus.ERROR.value,
                "approved_chapter_ids": ["chapter_001"],
            }

    video = Video()
    monkeypatch.setattr(api, "_unauthorized", lambda _request: None)
    monkeypatch.setattr(api, "_video_snapshot", lambda _video_id: video)
    dispatched = []
    monkeypatch.setattr(api, "render_final_chapters", lambda *_a: dispatched.append(True))

    response = client.post("/api/video/v_test/retry-finals")

    assert response.status_code == 200
    assert response.json()["status"] == VideoStatus.COMPLETED.value
    assert dispatched == []


def test_retry_finals_rejects_non_error_video(monkeypatch):
    class Video:
        exists = True
        def to_dict(self): return {"status": VideoStatus.PENDING_REVIEW.value}
    monkeypatch.setattr(api, "_unauthorized", lambda _request: None)
    monkeypatch.setattr(api, "_video_snapshot", lambda _video_id: Video())

    response = client.post("/api/video/v_test/retry-finals")
    assert response.status_code == 409
