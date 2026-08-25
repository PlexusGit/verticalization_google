from pathlib import Path
from unittest.mock import Mock

from flask import Flask, request
import pytest

import main


@pytest.fixture
def app():
    return Flask(__name__)


def invoke(app, method="POST", path="/", json=None, headers=None):
    with app.test_request_context(path, method=method, json=json, headers=headers or {}):
        return main.vertical_render(request)


def valid_payload():
    return {
        "video_id": "video_1", "chapter_id": "chapter_1", "render_id": "render_1",
        "source_gcs_path": "gs://input/source.mp4", "output_gcs_path": "gs://output/render.mp4",
        "trim_start": 0, "trim_end": 5, "video_resolution": {"w": 1920, "h": 1080},
        "mode": "center_crop",
    }


def test_health_and_options_do_not_require_auth(app, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "")
    body, status, _ = invoke(app, method="GET", path="/health")
    assert status == 200 and body["status"] == "healthy"
    _, status, headers = invoke(app, method="OPTIONS")
    assert status == 204 and "X-API-Key" in headers["Access-Control-Allow-Headers"]


def test_post_fails_closed_without_configured_or_matching_key(app, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "")
    assert invoke(app, json=valid_payload())[1] == 401
    monkeypatch.setattr(main, "API_KEY", "expected")
    assert invoke(app, json=valid_payload(), headers={"X-API-Key": "wrong"})[1] == 401


def test_invalid_request_never_starts_render(app, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "key")
    update = Mock()
    monkeypatch.setattr(main, "update_status", update)
    bad = valid_payload(); bad["trim_end"] = 0
    body, status, _ = invoke(app, json=bad, headers={"X-API-Key": "key"})
    assert status == 400 and "trim_end" in body["error"]
    update.assert_not_called()


def test_component_success_downloads_renders_uploads_and_updates_both_statuses(app, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "key")
    updates = []
    monkeypatch.setattr(main, "update_status", lambda *args, **kwargs: updates.append((args, kwargs)))

    def download(_uri, destination):
        Path(destination).write_bytes(b"source")

    def fake_render(_spec, input_path, output_path, **_kwargs):
        assert Path(input_path).exists()
        Path(output_path).write_bytes(b"mp4")

    monkeypatch.setattr(main, "_download", download)
    monkeypatch.setattr(main, "render", fake_render)
    monkeypatch.setattr(main, "_upload", lambda uri, source: "gs://output/render.mp4" if Path(source).exists() else None)

    body, status, _ = invoke(app, json=valid_payload(), headers={"X-API-Key": "key"})
    assert status == 200 and body["width"] == 1080 and body["height"] == 1920
    assert [entry[0][3] for entry in updates] == ["PROCESSING", "COMPLETED"]
    assert all(entry[0][2] == "chapter_1" for entry in updates)


def test_component_failure_records_error_without_leaking_details(app, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "key")
    updates = []
    monkeypatch.setattr(main, "update_status", lambda *args, **kwargs: updates.append((args, kwargs)))
    monkeypatch.setattr(main, "_download", lambda *_args: (_ for _ in ()).throw(RuntimeError("private details")))
    body, status, _ = invoke(app, json=valid_payload(), headers={"X-API-Key": "key"})
    assert status == 500 and body["error"] == "render failed"
    assert updates[-1][0][3] == "ERROR"
    assert "private details" in updates[-1][1]["error"]


def test_gcs_uri_and_default_bucket_parsing(monkeypatch):
    assert main.split_gcs_path("gs://bucket/folder/item.mp4") == ("bucket", "folder/item.mp4")
    monkeypatch.setattr(main, "DEFAULT_BUCKET", "default")
    assert main.split_gcs_path("folder/item.mp4") == ("default", "folder/item.mp4")
