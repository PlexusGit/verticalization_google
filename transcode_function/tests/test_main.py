from unittest.mock import Mock

import pytest
from flask import Flask, request

import main
from processor import TranscodeError


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SECURITY_TOKEN", "test-key")
    application = Flask(__name__)
    application.add_url_rule(
        "/", view_func=lambda: main.process_transcode(request),
        methods=["GET", "POST", "OPTIONS"],
    )
    return application.test_client()


def test_health_does_not_initialize_processor(app, monkeypatch):
    factory = Mock(side_effect=AssertionError("processor should remain lazy"))
    monkeypatch.setattr(main, "get_processor", factory)
    response = app.get("/")
    assert response.status_code == 200
    assert response.get_json()["status"] == "healthy"
    factory.assert_not_called()


def test_options_has_cors_headers(app):
    response = app.options("/")
    assert response.status_code == 204
    assert "X-API-Key" in response.headers["Access-Control-Allow-Headers"]


def test_post_requires_api_key(app):
    response = app.post("/", json={"video_id": "v1", "gcs_source_path": "in.mp4"})
    assert response.status_code == 401


def test_post_rejects_missing_fields(app):
    response = app.post("/", headers={"X-API-Key": "test-key"}, json={"video_id": "v1"})
    assert response.status_code == 400
    assert "gcs_source_path" in response.get_json()["error"]


def test_post_dispatches_to_processor(app, monkeypatch):
    processor = Mock()
    processor.process.return_value = {"video_id": "v1", "duration_seconds": 12.5}
    monkeypatch.setattr(main, "get_processor", lambda: processor)
    response = app.post(
        "/",
        headers={"X-API-Key": "test-key"},
        json={"video_id": "v1", "gcs_source_path": "uploads/in.mp4"},
    )
    assert response.status_code == 200
    assert response.get_json()["duration_seconds"] == 12.5
    processor.process.assert_called_once_with(
        video_id="v1", source_path="uploads/in.mp4", high_path=None, audio_prefix=None
    )


def test_expected_processing_error_is_422(app, monkeypatch):
    processor = Mock()
    processor.process.side_effect = TranscodeError("bad media")
    monkeypatch.setattr(main, "get_processor", lambda: processor)
    response = app.post(
        "/", headers={"X-API-Key": "test-key"},
        json={"video_id": "v1", "gcs_source_path": "input.mp4"},
    )
    assert response.status_code == 422
    assert response.get_json() == {"error": "bad media"}
