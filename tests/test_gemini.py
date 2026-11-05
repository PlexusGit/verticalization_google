import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from app.services import gemini, prompts
from app.services.gemini import ChapterSuggestions, SuggestedChapter


def test_gemini_response_schema_avoids_unsupported_numeric_keywords():
    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    schema_keys = keys(ChapterSuggestions.model_json_schema())
    assert "exclusiveMinimum" not in schema_keys
    assert "exclusiveMaximum" not in schema_keys


def test_suggested_chapter_validates_range_after_response_parsing():
    chapter = SuggestedChapter(start_seconds=10, end_seconds=25, title="Título", reason="Gancho")
    assert chapter.end_seconds == 25
    with pytest.raises(ValidationError):
        SuggestedChapter(start_seconds=-1, end_seconds=10, title="Título", reason="Gancho")
    with pytest.raises(ValidationError):
        SuggestedChapter(start_seconds=10, end_seconds=10, title="Título", reason="Gancho")


def test_suggest_chapters_keeps_the_client_alive_until_the_request_finishes(monkeypatch):
    """google-genai 2.x closes the HTTP client in ``Client.__del__``.

    ``genai.Client(...).models.generate_content(...)`` creates a temporary
    wrapper that is freed as soon as ``.models`` resolves — before the request
    runs — and the closed client then raises ``RuntimeError: Cannot send a
    request, as the client has been closed``. The fake below mimics the SDK:
    ``.models`` holds the API client but not the wrapper, so the wrapper is
    collectable mid-expression.
    """

    class FakeApiClient:
        closed = False

        def generate_content(self, model, contents, config):
            if self.closed:
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            return SimpleNamespace(parsed=gemini.ChapterSuggestions(chapters=[]))

    class FakeModels:
        def __init__(self, api_client):
            self._api_client = api_client

        def generate_content(self, *args, **kwargs):
            return self._api_client.generate_content(*args, **kwargs)

    class FakeClient:
        def __init__(self, api_key):
            self._api_client = FakeApiClient()
            self.models = FakeModels(self._api_client)

        def __del__(self):
            self._api_client.closed = True

    monkeypatch.setattr(gemini.settings, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini.genai, "Client", FakeClient)
    monkeypatch.setattr(prompts, "get_prompt", lambda _key: "PROMPT {minimum}-{maximum}")

    assert gemini.suggest_chapters("1\n00:00:00,000 --> 00:00:01,000\nolá\n") == []
