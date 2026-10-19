"""Unit tests for S3 event normalization, transfer, and API notification."""

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "process_s3_bucket_main", Path(__file__).resolve().parents[1] / "main.py"
)
main = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = main
_SPEC.loader.exec_module(main)


def event(payload):
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return SimpleNamespace(data={"message": {"data": data}})


def configure(monkeypatch):
    monkeypatch.setenv("S3_WATCH_BUCKET", "publisher-ingest")
    monkeypatch.setenv("S3_ALLOWED_EXTENSIONS", ".mp4,mov")
    monkeypatch.setenv("GCS_MAIN_BUCKET", "plexus-main")
    monkeypatch.setenv("S3_GCS_PREFIX", "incoming/s3")
    monkeypatch.setenv("MAIN_API_URL", "https://main.example")
    monkeypatch.setenv("API_TOKEN", "secret-value")


class FakeBody:
    def __init__(self, data):
        self.data, self.closed, self.position = data, False, 0

    def read(self, amount=-1):
        end = len(self.data) if amount is None or amount < 0 else self.position + amount
        chunk = self.data[self.position:end]
        self.position += len(chunk)
        return chunk

    def tell(self):
        return self.position

    def close(self):
        self.closed = True


class FakeBlob:
    def __init__(self, name, captured):
        self.name = name
        self.captured = captured
        self.metadata = None
        self.chunk_size = None
        self.upload = None

    def exists(self):
        return self.captured.get("exists", False)

    def upload_from_file(self, stream, **kwargs):
        self.upload = (stream.read(), kwargs)

    def delete(self):
        self.captured["deleted"] = self.name


class FakeStorageClient:
    def __init__(self, captured):
        self.captured = captured

    def bucket(self, name):
        self.captured["gcs_bucket"] = name
        return self

    def blob(self, name):
        blob = FakeBlob(name, self.captured)
        self.captured.setdefault("blob", blob)
        return blob


def install_cloud_fakes(monkeypatch, status=201, exists=False):
    captured = {"exists": exists}

    class FakeS3:
        def get_object(self, **kwargs):
            captured["get_object"] = kwargs
            body = FakeBody(b"video bytes")
            captured["body"] = body
            return {"Body": body, "ContentLength": 11}

    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: FakeS3())
    monkeypatch.setattr(main.storage, "Client", lambda: FakeStorageClient(captured))

    def fake_post(url, **kwargs):
        captured["post"] = (url, kwargs)
        return SimpleNamespace(status_code=status)

    monkeypatch.setattr(main.requests, "post", fake_post)
    return captured


def test_aws_record_decodes_key_copies_and_preserves_metadata(monkeypatch):
    configure(monkeypatch)
    captured = install_cloud_fakes(monkeypatch)
    payload = {
        "Records": [
            {
                "eventName": "ObjectCreated:Put",
                "eventTime": "2025-02-03T10:20:30Z",
                "awsRegion": "us-east-1",
                "s3": {
                    "bucket": {"name": "publisher-ingest"},
                    "object": {
                        "key": "daily/My+Clip%20One.MP4",
                        "size": 123,
                        "eTag": "abc123",
                        "versionId": "version-7",
                        "sequencer": "001",
                    },
                },
            }
        ]
    }

    main.process_s3_bucket(event(payload))

    assert captured["get_object"] == {
        "Bucket": "publisher-ingest",
        "Key": "daily/My Clip One.MP4",
        "VersionId": "version-7",
    }
    assert captured["body"].closed
    assert captured["gcs_bucket"] == "plexus-main"
    blob = captured["blob"]
    assert blob.name.startswith("incoming/s3/publisher-ingest/")
    assert blob.name.endswith("/My_Clip_One.MP4")
    assert blob.upload[0] == b"video bytes"
    assert blob.upload[1]["if_generation_match"] == 0
    assert blob.upload[1]["size"] == 11
    assert blob.chunk_size == main.UPLOAD_CHUNK_BYTES
    assert blob.metadata["source-version-id"] == "version-7"
    assert captured["deleted"] == blob.name

    url, request = captured["post"]
    assert url == "https://main.example/api/import/gcs"
    assert request["headers"]["X-API-Key"] == "secret-value"
    assert request["json"]["source_metadata"]["key"] == "daily/My Clip One.MP4"
    assert request["json"]["source_metadata"]["version_id"] == "version-7"
    assert request["json"]["source_metadata"]["etag"] == "abc123"
    assert request["json"]["gcs_uri"].startswith("gs://plexus-main/incoming/s3/")


def test_canonical_payload_supported_and_path_is_deterministic(monkeypatch):
    configure(monkeypatch)
    captured = install_cloud_fakes(monkeypatch)
    payload = {
        "bucket": "publisher-ingest",
        "key": "folder%2Fclip.mov",
        "version_id": "v1",
        "etag": "etag-v1",
    }

    source = next(main._iter_s3_objects(payload))
    assert source["key"] == "folder/clip.mov"
    assert main._destination_path(source) == main._destination_path(source)
    main.process_s3_bucket(event(payload))
    assert captured["get_object"]["Key"] == "folder/clip.mov"


@pytest.mark.parametrize(
    "payload",
    [
        {"bucket": "another-bucket", "key": "clip.mp4"},
        {"bucket": "publisher-ingest", "key": "document.pdf"},
        {
            "Records": [
                {
                    "eventName": "ObjectRemoved:Delete",
                    "s3": {
                        "bucket": {"name": "publisher-ingest"},
                        "object": {"key": "clip.mp4"},
                    },
                }
            ]
        },
    ],
)
def test_irrelevant_events_are_ignored(monkeypatch, payload):
    configure(monkeypatch)
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: pytest.fail("no download"))
    main.process_s3_bucket(event(payload))


def test_api_failure_is_raised_for_pubsub_retry(monkeypatch):
    configure(monkeypatch)
    install_cloud_fakes(monkeypatch)
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(status_code=429),
    )
    with pytest.raises(RuntimeError, match="HTTP 429"):
        main.process_s3_bucket(event({"bucket": "publisher-ingest", "key": "clip.mp4"}))


@pytest.mark.parametrize("status", [400, 404, 413, 415, 422])
def test_permanent_api_rejection_is_acknowledged(monkeypatch, status):
    configure(monkeypatch)
    captured = install_cloud_fakes(monkeypatch, status=status)
    main.process_s3_bucket(event({"bucket": "publisher-ingest", "key": "clip.mp4"}))
    assert "post" in captured
    # The import will never be registered, so the staging copy is cleaned up.
    assert captured["deleted"] == captured["blob"].name


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 503])
def test_transient_api_errors_are_retried(monkeypatch, status):
    configure(monkeypatch)
    captured = install_cloud_fakes(monkeypatch, status=status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        main.process_s3_bucket(event({"bucket": "publisher-ingest", "key": "clip.mp4"}))
    assert "deleted" not in captured


def test_existing_destination_skips_transfer_but_still_notifies(monkeypatch):
    configure(monkeypatch)
    captured = install_cloud_fakes(monkeypatch, exists=True)
    main.process_s3_bucket(event({"bucket": "publisher-ingest", "key": "clip.mp4"}))
    assert "get_object" not in captured
    assert "post" in captured
    assert captured["deleted"] == captured["blob"].name


def test_failed_stream_never_notifies_the_api(monkeypatch):
    configure(monkeypatch)
    captured = install_cloud_fakes(monkeypatch)

    def broken_upload(self, stream, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(FakeBlob, "upload_from_file", broken_upload)
    with pytest.raises(OSError):
        main.process_s3_bucket(event({"bucket": "publisher-ingest", "key": "clip.mp4"}))
    assert "post" not in captured
    assert captured["body"].closed


def test_cleanup_failure_does_not_fail_the_delivery(monkeypatch):
    configure(monkeypatch)
    captured = install_cloud_fakes(monkeypatch)

    def failing_delete(self):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(FakeBlob, "delete", failing_delete)
    main.process_s3_bucket(event({"bucket": "publisher-ingest", "key": "clip.mp4"}))
    assert "post" in captured


def test_default_extensions_match_the_api():
    assert main._configured_extensions() >= {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mxf"}
    assert not {".m4v", ".mpg", ".mpeg"} & main._configured_extensions()
