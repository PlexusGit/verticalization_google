import pytest
from pydantic import ValidationError

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
