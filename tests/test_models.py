import pytest
from pydantic import ValidationError

from app.models import AllowedUserInput, ChapterInput, ImportGCSRequest, RenderRequest


def test_allowed_user_normalizes_and_validates_email():
    assert AllowedUserInput(email=" Person@Example.com ").email == "person@example.com"
    with pytest.raises(ValidationError):
        AllowedUserInput(email="not-an-email")


def test_chapter_requires_forward_time_range():
    with pytest.raises(ValidationError):
        ChapterInput(title="Teste", start_seconds=20, end_seconds=10)


def test_render_defaults_to_centered_static_caption_style():
    request = RenderRequest()
    assert request.layout_mode == "center_crop"
    assert request.caption_style.position == "bottom"
    assert request.branding.logo is None


def test_colors_are_normalized_and_validated():
    request = RenderRequest.model_validate({"caption_style": {"color": "#abcdef"}})
    assert request.caption_style.color == "#ABCDEF"
    with pytest.raises(ValidationError):
        RenderRequest.model_validate({"caption_style": {"color": "red;filter"}})


def test_importer_canonical_payload_is_normalized():
    payload = ImportGCSRequest.model_validate({
        "source": "s3",
        "bucket": "managed-bucket",
        "object_name": "imports/example.mp4",
        "idempotency_key": "s3-abc",
        "source_metadata": {"key": "source/example.mp4"},
    })
    assert payload.path == "imports/example.mp4"
    assert payload.source_type == "s3"
    assert payload.source_id == "s3-abc"
