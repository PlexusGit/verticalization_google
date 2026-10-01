import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_is_public():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "plexus-vertical-cuts"


def test_api_requires_session_or_key():
    response = client.get("/api/videos")
    assert response.status_code == 401
    assert response.json()["error"] == "Não autorizado"


def test_dashboard_redirects_to_login():
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_only_configured_logo_is_served():
    response = client.get("/logo.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["content-type"]


def test_authenticated_templates_render_in_pt_br(monkeypatch):
    from app.routers import views

    user = {"id": "test", "info": {"name": "Pessoa", "email": "pessoa@example.com", "role": "admin"}}
    monkeypatch.setattr(views, "get_current_user", lambda _request: user)
    monkeypatch.setattr(views, "request_is_admin", lambda _request: True)
    dashboard = client.get("/dashboard")
    detail = client.get("/video/v_test")
    users = client.get("/users")
    templates = client.get("/templates")
    assert dashboard.status_code == 200
    assert "Cortes verticais, com revisão humana" in dashboard.text
    assert 'data-video-id="${escapeHtml(v.id)}"' in dashboard.text
    assert "video.currentTime=Math.min(1" in dashboard.text
    assert detail.status_code == 200
    assert "Editor vertical" in detail.text
    assert "Foco automático em rostos" in detail.text
    assert 'id="editor-modal"' in detail.text
    assert 'data-tab="editor"' not in detail.text
    assert 'id="tab-editor"' not in detail.text
    assert 'src="/logo.svg"' in detail.text
    assert 'id="approve" disabled' in detail.text
    assert 'class="chapters-review-layout"' in detail.text
    assert 'class="actions chapter-actions"' in detail.text
    assert users.status_code == 200
    assert "Adicionar usuário" in users.text
    assert templates.status_code == 200
    assert "Criar template" in templates.text
    logo_markup = re.search(r'id="canvas-logo"(.*?)id="canvas-title"', templates.text, re.DOTALL)
    assert logo_markup is not None
    assert logo_markup.group(1).count('class="resize-handle') == 1


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is unavailable")
def test_rendered_inline_javascript_is_syntactically_valid(monkeypatch):
    from app.routers import views

    user = {"id": "test", "info": {"name": "Pessoa", "email": "pessoa@example.com", "role": "admin"}}
    monkeypatch.setattr(views, "get_current_user", lambda _request: user)
    monkeypatch.setattr(views, "request_is_admin", lambda _request: True)
    pages = [client.get("/dashboard").text, client.get("/video/v_test").text, client.get("/users").text, client.get("/templates").text]
    for page in pages:
        scripts = [source for attrs, source in re.findall(r"<script([^>]*)>(.*?)</script>", page, re.DOTALL) if "src=" not in attrs]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inline.js"
            path.write_text("\n".join(scripts), encoding="utf-8")
            subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, text=True)
