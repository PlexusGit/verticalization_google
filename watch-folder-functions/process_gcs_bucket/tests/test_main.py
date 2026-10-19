"""Unit tests for the GCS Pub/Sub importer (no cloud services required)."""

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


def event(payload, attributes=None):
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return SimpleNamespace(data={"message": {"data": encoded, "attributes": attributes or {}}})


def configure(monkeypatch):
    monkeypatch.setenv("GCS_WATCH_BUCKET", "incoming-videos")
    monkeypatch.setenv("GCS_ALLOWED_EXTENSIONS", "mp4,.MOV")
    monkeypatch.setenv("MAIN_API_URL", "https://main.example/")
    monkeypatch.setenv("API_TOKEN", "secret-value")


def test_finalize_posts_deterministic_metadata(monkeypatch):
    configure(monkeypatch)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(main.requests, "post", fake_post)
    payload = {
        "bucket": "incoming-videos",
        "name": "news/Clip.MP4",
        "generation": "1700",
        "metageneration": "1",
        "etag": "etag-one",
        "size": "42",
        "contentType": "video/mp4",
        "timeCreated": "2025-01-01T00:00:00Z",
    }

    main.process_gcs_bucket(event(payload, {"eventType": "OBJECT_FINALIZE"}))

    assert len(calls) == 1
    url, request = calls[0]
    assert url == "https://main.example/api/import/gcs"
    assert request["headers"]["X-API-Key"] == "secret-value"
    assert request["headers"]["Idempotency-Key"].startswith("gcs-")
    assert request["json"]["gcs_uri"] == "gs://incoming-videos/news/Clip.MP4"
    assert request["json"]["source_metadata"] == {
        "provider": "gcs",
        "bucket": "incoming-videos",
        "key": "news/Clip.MP4",
        "generation": "1700",
        "metageneration": "1",
        "etag": "etag-one",
        "size": "42",
        "content_type": "video/mp4",
        "md5_hash": "",
        "crc32c": "",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "",
        "custom_metadata": {},
    }
    assert main._build_api_payload(payload) == main._build_api_payload(payload)


@pytest.mark.parametrize(
    "payload,attributes",
    [
        ({"bucket": "other", "name": "clip.mp4"}, {}),
        ({"bucket": "incoming-videos", "name": "notes.txt"}, {}),
        ({"bucket": "incoming-videos", "name": "clip.mp4"}, {"eventType": "OBJECT_DELETE"}),
    ],
)
def test_irrelevant_events_are_ignored(monkeypatch, payload, attributes):
    configure(monkeypatch)
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: pytest.fail("must not call API"))
    main.process_gcs_bucket(event(payload, attributes))


def test_malformed_message_is_ignored(monkeypatch):
    configure(monkeypatch)
    cloud_event = SimpleNamespace(data={"message": {"data": "not base64"}})
    main.process_gcs_bucket(cloud_event)


def test_api_failure_is_raised_for_pubsub_retry(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(status_code=503),
    )
    with pytest.raises(RuntimeError, match="HTTP 503"):
        main.process_gcs_bucket(event({"bucket": "incoming-videos", "name": "clip.mp4"}))
