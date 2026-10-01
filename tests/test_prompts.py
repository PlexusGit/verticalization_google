"""Admin prompt overrides: Firestore persistence, revert, and Gemini use."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.routers import api
from app.services import gemini, prompts


client = TestClient(app)


class FakeDoc:
    def __init__(self):
        self._data = None

    def get(self):
        data = self._data
        return SimpleNamespace(exists=data is not None, to_dict=lambda: dict(data or {}))

    def set(self, data):
        self._data = dict(data)

    def delete(self):
        self._data = None


class FakePromptsDB:
    def __init__(self):
        self.documents = {}

    def collection(self, _name):
        return self

    def document(self, key):
        return self.documents.setdefault(key, FakeDoc())


def _fake_db(monkeypatch):
    db = FakePromptsDB()
    monkeypatch.setattr(prompts, "get_db", lambda: db)
    return db


def test_get_prompt_falls_back_to_default(monkeypatch):
    _fake_db(monkeypatch)
    assert prompts.get_prompt(prompts.CHAPTER_SUGGESTIONS) == prompts.DEFAULT_CHAPTER_PROMPT


def test_save_and_revert_prompt(monkeypatch):
    db = _fake_db(monkeypatch)
    prompts.save_prompt(prompts.CHAPTER_SUGGESTIONS, "prompt editado", "admin@example.com")

    assert prompts.get_prompt(prompts.CHAPTER_SUGGESTIONS) == "prompt editado"
    doc = db.documents[prompts.CHAPTER_SUGGESTIONS]._data
    assert doc["updated_by"] == "admin@example.com"

    prompts.revert_prompt(prompts.CHAPTER_SUGGESTIONS)
    assert prompts.get_prompt(prompts.CHAPTER_SUGGESTIONS) == prompts.DEFAULT_CHAPTER_PROMPT


def test_list_prompts_marks_edited(monkeypatch):
    _fake_db(monkeypatch)
    prompts.save_prompt(prompts.CHAPTER_SUGGESTIONS, "editado", None)

    listed = {item["key"]: item for item in prompts.list_prompts()}
    assert listed[prompts.CHAPTER_SUGGESTIONS]["edited"] is True
    assert listed[prompts.CHAPTER_SUGGESTIONS]["current"] == "editado"
    assert listed[prompts.CHAPTER_SUGGESTIONS]["default"] == prompts.DEFAULT_CHAPTER_PROMPT


def _admin_mocks(monkeypatch):
    monkeypatch.setattr(api, "get_current_user", lambda _request: {"info": {"email": "admin@example.com", "role": "admin"}})
    monkeypatch.setattr(api, "request_is_admin", lambda _request: True)


def test_prompts_endpoints_round_trip(monkeypatch):
    _fake_db(monkeypatch)
    _admin_mocks(monkeypatch)

    response = client.get("/api/prompts")
    assert response.status_code == 200
    item = response.json()["prompts"][0]
    assert item["key"] == prompts.CHAPTER_SUGGESTIONS
    assert item["edited"] is False

    assert client.put("/api/prompts/chapter_suggestions", json={"text": "versão customizada"}).status_code == 200
    item = client.get("/api/prompts").json()["prompts"][0]
    assert item["edited"] is True
    assert item["current"] == "versão customizada"

    assert client.delete("/api/prompts/chapter_suggestions").status_code == 200
    item = client.get("/api/prompts").json()["prompts"][0]
    assert item["edited"] is False
    assert item["current"] == item["default"]


def test_prompts_unknown_key_returns_404(monkeypatch):
    _fake_db(monkeypatch)
    _admin_mocks(monkeypatch)
    assert client.put("/api/prompts/ghost", json={"text": "x"}).status_code == 404
    assert client.delete("/api/prompts/ghost").status_code == 404


def test_prompts_require_admin(monkeypatch):
    _fake_db(monkeypatch)
    monkeypatch.setattr(api, "get_current_user", lambda _request: {"info": {"email": "u@example.com", "role": "user"}})
    monkeypatch.setattr(api, "request_is_admin", lambda _request: False)
    assert client.get("/api/prompts").status_code == 403
    assert client.put("/api/prompts/chapter_suggestions", json={"text": "x"}).status_code == 403


def test_suggest_chapters_uses_edited_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(gemini.settings, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(prompts, "get_prompt", lambda _key: "PROMPT EDITADO {minimum}-{maximum}")

    class FakeModels:
        def generate_content(self, model, contents, config):
            captured["contents"] = contents
            return SimpleNamespace(parsed=gemini.ChapterSuggestions(chapters=[]))

    monkeypatch.setattr(gemini.genai, "Client", lambda api_key: SimpleNamespace(models=FakeModels()))

    gemini.suggest_chapters("1\n00:00:00,000 --> 00:00:10,000\nOlá\n")

    assert "PROMPT EDITADO" in captured["contents"]
    assert f"{gemini.settings.DEFAULT_CHAPTER_MIN_SECONDS}-{gemini.settings.DEFAULT_CHAPTER_MAX_SECONDS}" in captured["contents"]


def test_suggest_chapters_falls_back_on_broken_placeholders(monkeypatch):
    captured = {}
    monkeypatch.setattr(gemini.settings, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(prompts, "get_prompt", lambda _key: "quebrado {unknown}")

    class FakeModels:
        def generate_content(self, model, contents, config):
            captured["contents"] = contents
            return SimpleNamespace(parsed=gemini.ChapterSuggestions(chapters=[]))

    monkeypatch.setattr(gemini.genai, "Client", lambda api_key: SimpleNamespace(models=FakeModels()))

    gemini.suggest_chapters("x")

    assert "quebrado" not in captured["contents"]
    assert "editor de vídeos jornalísticos" in captured["contents"]
