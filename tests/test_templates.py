from fastapi.testclient import TestClient

from app.main import app
from pydantic import ValidationError
import pytest

from app.models import RenderRequest, VideoTemplateInput
from app.routers import api, views
from app.services import processing


client = TestClient(app)


class Snapshot:
    def __init__(self, data=None, exists=True):
        self.data = data or {}
        self.exists = exists

    def to_dict(self):
        return self.data


class Reference:
    def __init__(self, document_id, snapshot=None):
        self.id = document_id
        self.snapshot = snapshot or Snapshot(exists=False)
        self.written = None

    def get(self):
        return self.snapshot

    def set(self, data, merge=False):
        self.written = data


class Collection:
    def __init__(self, documents=None):
        self.documents = documents or []
        self.references = {}

    def stream(self):
        return self.documents

    def document(self, document_id=None):
        document_id = document_id or "template_new"
        return self.references.setdefault(document_id, Reference(document_id))


class DB:
    def __init__(self, collection):
        self.value = collection

    def collection(self, _name):
        return self.value


def test_template_model_and_macro_branding_are_validated():
    template = VideoTemplateInput(title="Notícias", title_macro="{video_title} — {chapter_title}", title_position="top-left")
    branding = api._template_branding(
        {**template.model_dump(), "logo": {"gcs_path": "templates/logo.png", "x_percent": 68, "y_percent": 5, "width_percent": 27, "height_percent": 10}, "title_geometry": {"x_percent": 10, "y_percent": 18, "width_percent": 80, "height_percent": 12, "font_size": 54}},
        {"title": "Jornal"},
        {"title": "Economia"},
    )
    options = RenderRequest.model_validate({"branding": branding})
    assert options.branding.text.text == "Jornal — Economia"
    assert options.branding.text.position == "top-left"
    assert options.branding.logo.gcs_path == "templates/logo.png"
    assert options.branding.logo.x_percent == 68
    assert options.branding.text.width_percent == 80


def test_render_payload_converts_template_percentages_to_pixels(monkeypatch):
    class Document:
        exists = True

        def __init__(self, data=None):
            self.data = data or {}

        def get(self):
            return self

        def to_dict(self):
            return self.data

        def set(self, _data):
            return None

        def update(self, data):
            self.data.update(data)

        def collection(self, _name):
            return self

        def document(self, _document_id):
            return render_document

    render_document = Document()
    chapter_document = Document({"start_seconds": 10, "end_seconds": 20, "subtitle_text": ""})
    chapter_document.document = lambda _document_id: render_document
    video_document = Document({"gcs_high_path": "video/high.mp4", "video_resolution": {"w": 1920, "h": 1080}})
    video_document.document = lambda _chapter_id: chapter_document
    captured = {}
    monkeypatch.setattr(processing, "_video_ref", lambda _video_id: video_document)
    monkeypatch.setattr(processing.vertical_render, "submit", lambda payload: captured.update(payload) or {"gcs_path": payload["target_gcs_path"], "automatic_focal_points": [{"time": 10, "x": 900, "y": 540}]})
    options = RenderRequest.model_validate({"branding": {"logo": {"gcs_path": "templates/logo.png", "x_percent": 68, "y_percent": 5, "width_percent": 27, "height_percent": 10}, "text": {"text": "Título de capítulo suficientemente longo", "x_percent": 10, "y_percent": 18, "width_percent": 80, "height_percent": 12, "font_size": 54}}})

    processing.render_chapter("video", "chapter", options, final=True)

    assert captured["logo"] | {"opacity": 1} == {"gcs_path": "templates/logo.png", "position": "top_right", "size": 292, "width": 292, "height": 192, "x": 734, "y": 96, "opacity": 1}
    assert captured["text_overlays"][0]["x"] == 108
    assert captured["text_overlays"][0]["y"] == 346
    assert captured["text_overlays"][0]["max_width"] == 864
    assert chapter_document.data["render_options"]["automatic_focal_points"] == [{"time": 10, "x": 900, "y": 540}]


def test_template_geometry_must_stay_inside_vertical_canvas():
    template = VideoTemplateInput(
        title="Geometria",
        logo_x_percent=62,
        logo_y_percent=8,
        logo_width_percent=30,
        logo_height_percent=12,
        title_x_percent=8,
        title_y_percent=20,
        title_width_percent=84,
        title_height_percent=15,
    )
    assert template.title_width_percent == 84
    with pytest.raises(ValidationError):
        VideoTemplateInput(title="Inválido", title_x_percent=80, title_width_percent=30)


def test_authenticated_user_can_list_and_create_template(monkeypatch):
    existing = Reference("template_a", Snapshot({"id": "template_a", "title": "A", "title_macro": "{chapter_title}"}))
    existing.id = "template_a"
    existing.to_dict = existing.snapshot.to_dict
    collection = Collection([existing])
    monkeypatch.setattr(api, "request_is_authenticated", lambda _request: True)
    monkeypatch.setattr(api, "get_db", lambda: DB(collection))
    monkeypatch.setattr(api, "get_current_user", lambda _request: {"info": {"email": "editor@example.com"}})

    listed = client.get("/api/templates")
    created = client.post(
        "/api/templates",
        data={"title": "Plantão", "title_macro": "{chapter_title}", "logo_position": "top-right", "title_position": "top-center", "title_x_percent": 12, "title_y_percent": 22, "title_width_percent": 76, "title_height_percent": 14, "title_font_size": 60},
    )

    assert listed.status_code == 200
    assert listed.json()["templates"][0]["id"] == "template_a"
    assert created.status_code == 201
    written = collection.references["template_new"].written
    assert written["created_by"] == "editor@example.com"
    assert written["title_geometry"] == {"x_percent": 12.0, "y_percent": 22.0, "width_percent": 76.0, "height_percent": 14.0, "font_size": 60}


def test_templates_page_requires_login_and_renders_for_user(monkeypatch):
    monkeypatch.setattr(views, "get_current_user", lambda _request: None)
    denied = client.get("/templates", follow_redirects=False)
    monkeypatch.setattr(views, "get_current_user", lambda _request: {"id": "user", "info": {"email": "user@example.com", "role": "user"}})
    allowed = client.get("/templates")
    assert denied.status_code == 303
    assert allowed.status_code == 200
    assert "Criar template" in allowed.text
    assert 'href="/templates"' in allowed.text


class TemplateDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.reference = self
        self._data = dict(data)
        self.exists = bool(data)
        self.written = None
        self.updates = []

    def to_dict(self):
        return self._data

    def get(self):
        return self

    def set(self, data, merge=False):
        if merge:
            self._data.update(data)
        else:
            self._data = data
        self.written = data

    def update(self, data):
        self.updates.append(data)
        self._data.update(data)


class TemplateCollection:
    def __init__(self, docs=None):
        self.docs = {doc.id: doc for doc in (docs or [])}
        self.new_reference = TemplateDoc("template_new", {})

    def document(self, doc_id=None):
        if doc_id is None:
            return self.new_reference
        return self.docs.setdefault(doc_id, TemplateDoc(doc_id, {}))

    def stream(self):
        return list(self.docs.values())


class TemplateDB:
    def __init__(self, collection):
        self.collection_obj = collection

    def collection(self, _name):
        return self.collection_obj


def test_update_template_edits_fields_and_keeps_logo(monkeypatch):
    existing = TemplateDoc(
        "tpl_1",
        {
            "id": "tpl_1",
            "title": "Antigo",
            "title_macro": "{chapter_title}",
            "logo": {"gcs_path": "templates/tpl_1/logo.png", "x_percent": 68, "y_percent": 5, "width_percent": 27, "height_percent": 10},
            "is_default": False,
        },
    )
    collection = TemplateCollection([existing])
    monkeypatch.setattr(api, "request_is_authenticated", lambda _request: True)
    monkeypatch.setattr(api, "get_db", lambda: TemplateDB(collection))

    response = client.put(
        "/api/templates/tpl_1",
        data={"title": "Novo nome", "title_macro": "{video_title}: {chapter_title}", "title_x_percent": 5, "title_y_percent": 10, "title_width_percent": 90, "title_height_percent": 10, "title_font_size": 70, "logo_x_percent": 50, "is_default": "true"},
    )

    assert response.status_code == 200
    assert existing.written["title"] == "Novo nome"
    assert existing.written["is_default"] is True
    assert existing.written["title_geometry"]["font_size"] == 70
    # Logo kept, geometry updated from the form.
    assert existing.written["logo"]["gcs_path"] == "templates/tpl_1/logo.png"
    assert existing.written["logo"]["x_percent"] == 50


def test_update_template_returns_404_for_missing_template(monkeypatch):
    monkeypatch.setattr(api, "request_is_authenticated", lambda _request: True)
    monkeypatch.setattr(api, "get_db", lambda: TemplateDB(TemplateCollection()))

    response = client.put("/api/templates/ghost", data={"title": "X"})

    assert response.status_code == 404


def test_setting_default_template_unsets_others(monkeypatch):
    old_default = TemplateDoc("tpl_old", {"id": "tpl_old", "title": "Velho padrão", "is_default": True})
    other = TemplateDoc("tpl_other", {"id": "tpl_other", "title": "Outro", "is_default": True})
    collection = TemplateCollection([old_default, other])
    monkeypatch.setattr(api, "request_is_authenticated", lambda _request: True)
    monkeypatch.setattr(api, "get_db", lambda: TemplateDB(collection))
    monkeypatch.setattr(api, "get_current_user", lambda _request: {"info": {"email": "editor@example.com"}})

    response = client.post("/api/templates", data={"title": "Novo padrão", "title_macro": "{chapter_title}", "is_default": "true"})

    assert response.status_code == 201
    assert collection.new_reference.written["is_default"] is True
    # Every other default flag was cleared — only one default may exist.
    for doc in (old_default, other):
        assert any(update.get("is_default") is False for update in doc.updates)


def test_create_template_without_default_flag_is_not_default(monkeypatch):
    collection = TemplateCollection()
    monkeypatch.setattr(api, "request_is_authenticated", lambda _request: True)
    monkeypatch.setattr(api, "get_db", lambda: TemplateDB(collection))
    monkeypatch.setattr(api, "get_current_user", lambda _request: {"info": {"email": "editor@example.com"}})

    response = client.post("/api/templates", data={"title": "Simples", "title_macro": "{chapter_title}"})

    assert response.status_code == 201
    assert collection.new_reference.written["is_default"] is False


class _RenderChapter:
    def __init__(self, data=None):
        self.id = "chapter_001"
        self.reference = self
        self.exists = True
        self.written = None
        self._data = {"id": "chapter_001", "title": "Capítulo", **(data or {})}

    def to_dict(self):
        return self._data

    def get(self):
        return self

    def set(self, data, merge=False):
        if merge:
            self._data.update(data)
        self.written = data


def _render_mocks(monkeypatch, chapter, template_docs):
    """Wire the render endpoint to fakes; returns the dispatched options list."""
    dispatched = []

    class Chapters:
        def document(self, _id):
            return chapter

    class VideoRef:
        def collection(self, _name):
            return Chapters()

    class Video:
        exists = True
        reference = VideoRef()

        def to_dict(self):
            return {"title": "Jornal", "status": "PENDING_REVIEW"}

    monkeypatch.setattr(api, "request_is_authenticated", lambda _request: True)
    monkeypatch.setattr(api, "_video_snapshot", lambda _video_id: Video())
    monkeypatch.setattr(api, "get_db", lambda: TemplateDB(TemplateCollection(template_docs)))
    monkeypatch.setattr(api, "render_chapter", lambda *args, **kwargs: dispatched.append((args, kwargs)))
    return dispatched


def test_render_resolves_explicit_template_into_branding(monkeypatch):
    chapter = _RenderChapter()
    template = TemplateDoc(
        "tpl_1",
        {
            "id": "tpl_1",
            "title": "Plantão",
            "title_macro": "{video_title}: {chapter_title}",
            "title_position": "top-center",
            "title_geometry": {"x_percent": 10, "y_percent": 18, "width_percent": 80, "height_percent": 12, "font_size": 60},
            "logo": {"gcs_path": "templates/tpl_1/logo.png", "x_percent": 50, "y_percent": 40, "width_percent": 30, "height_percent": 15},
        },
    )
    dispatched = _render_mocks(monkeypatch, chapter, [template])

    response = client.post("/api/video/v_test/chapters/chapter_001/render", json={"template_id": "tpl_1"})

    assert response.status_code == 202
    options = dispatched[0][0][2]
    # Draft previews render unbranded; the template is applied once at approval.
    assert options.branding.logo is None
    assert options.branding.text is None
    # The chapter keeps the raw explicit choice and the snapshot.
    assert chapter.written["template_id"] == "tpl_1"
    assert chapter.written["template_title"] == "Plantão"
    assert chapter.written["template_snapshot"]["id"] == "tpl_1"
    # The stored render_options do not carry template_id.
    assert chapter.written["render_options"]["template_id"] is None


def test_render_auto_choice_uses_default_template(monkeypatch):
    chapter = _RenderChapter()
    default = TemplateDoc("tpl_d", {"id": "tpl_d", "title": "Padrão", "title_macro": "{chapter_title}", "is_default": True, "logo": None})
    dispatched = _render_mocks(monkeypatch, chapter, [default])

    response = client.post("/api/video/v_test/chapters/chapter_001/render", json={"template_id": ""})

    assert response.status_code == 202
    assert dispatched[0][0][2].branding.text is None
    # Empty choice stays auto so the chapter follows the default template.
    assert chapter.written["template_id"] is None
    assert chapter.written["template_snapshot"]["id"] == "tpl_d"


def test_render_none_choice_clears_branding(monkeypatch):
    chapter = _RenderChapter({"render_options": {"branding": {"text": {"text": "old"}}}})
    default = TemplateDoc("tpl_d", {"id": "tpl_d", "title": "Padrão", "is_default": True, "logo": None})
    dispatched = _render_mocks(monkeypatch, chapter, [default])

    response = client.post("/api/video/v_test/chapters/chapter_001/render", json={"template_id": "none"})

    assert response.status_code == 202
    options = dispatched[0][0][2]
    assert options.branding.logo is None
    assert options.branding.text is None
    assert chapter.written["template_id"] == "none"
    assert chapter.written["template_snapshot"] is None


def test_render_final_resolves_template_into_branding(monkeypatch):
    chapter = _RenderChapter()
    template = TemplateDoc(
        "tpl_1",
        {
            "id": "tpl_1",
            "title": "Plantão",
            "title_macro": "{chapter_title}",
            "logo": {"gcs_path": "templates/tpl_1/logo.png", "x_percent": 50, "y_percent": 40, "width_percent": 30, "height_percent": 15},
        },
    )
    dispatched = _render_mocks(monkeypatch, chapter, [template])

    response = client.post("/api/video/v_test/chapters/chapter_001/render", json={"template_id": "tpl_1", "final": True})

    assert response.status_code == 202
    options = dispatched[0][0][2]
    assert options.branding.text.text == "Capítulo"
    assert options.branding.logo.x_percent == 50


def test_draft_render_preserves_stored_branding(monkeypatch):
    stored = {"branding": {"text": {"text": "approved title"}}, "camera_plan_gcs_path": "plan.json.gz"}
    chapter = _RenderChapter({"render_options": stored})
    dispatched = _render_mocks(monkeypatch, chapter, [])

    response = client.post("/api/video/v_test/chapters/chapter_001/render", json={})

    assert response.status_code == 202
    # The dispatched preview stays unbranded…
    assert dispatched[0][0][2].branding.text is None
    # …but the chapter's stored options keep the approval-owned artifacts.
    persisted = chapter.written["render_options"]
    assert persisted["branding"]["text"]["text"] == "approved title"
    assert persisted["camera_plan_gcs_path"] == "plan.json.gz"


def test_render_rejects_missing_template(monkeypatch):
    chapter = _RenderChapter()
    dispatched = _render_mocks(monkeypatch, chapter, [])

    response = client.post("/api/video/v_test/chapters/chapter_001/render", json={"template_id": "ghost"})

    assert response.status_code == 400
    assert dispatched == []
