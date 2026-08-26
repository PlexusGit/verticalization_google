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

        def update(self, _data):
            return None

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
    monkeypatch.setattr(processing.vertical_render, "submit", lambda payload: captured.update(payload) or {"gcs_path": payload["target_gcs_path"]})
    options = RenderRequest.model_validate({"branding": {"logo": {"gcs_path": "templates/logo.png", "x_percent": 68, "y_percent": 5, "width_percent": 27, "height_percent": 10}, "text": {"text": "Título de capítulo suficientemente longo", "x_percent": 10, "y_percent": 18, "width_percent": 80, "height_percent": 12, "font_size": 54}}})

    processing.render_chapter("video", "chapter", options, final=True)

    assert captured["logo"] | {"opacity": 1} == {"gcs_path": "templates/logo.png", "position": "top_right", "size": 292, "width": 292, "height": 192, "x": 734, "y": 96, "opacity": 1}
    assert captured["text_overlays"][0]["x"] == 108
    assert captured["text_overlays"][0]["y"] == 346
    assert captured["text_overlays"][0]["max_width"] == 864


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
