"""Unit tests for S3 event normalization, transfer, and API notification."""

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


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


class FakeBlob:
    def __init__(self, name):
        self.name = name
        self.metadata = None
        self.upload = None

    def upload_from_filename(self, filename, **kwargs):
        self.upload = (Path(filename).read_bytes(), kwargs)


class FakeStorageClient:
    def __init__(self, captured):
        self.captured = captured

    def bucket(self, name):
        self.captured["gcs_bucket"] = name
        return self

    def blob(self, name):
        blob = FakeBlob(name)
        self.captured["blob"] = blob
        return blob


def install_cloud_fakes(monkeypatch):
    captured = {}

    class FakeS3:
        def download_file(self, bucket, key, filename, **kwargs):
            captured["download"] = (bucket, key, kwargs)
            Path(filename).write_bytes(b"video bytes")

    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: FakeS3())
    monkeypatch.setattr(main.storage, "Client", lambda: FakeStorageClient(captured))

    def fake_post(url, **kwargs):
        captured["post"] = (url, kwargs)
        return SimpleNamespace(status_code=201)

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

    assert captured["download"] == (
        "publisher-ingest",
        "daily/My Clip One.MP4",
        {"ExtraArgs": {"VersionId": "version-7"}},
    )
    assert captured["gcs_bucket"] == "plexus-main"
    blob = captured["blob"]
    assert blob.name.startswith("incoming/s3/publisher-ingest/")
    assert blob.name.endswith("/My_Clip_One.MP4")
    assert blob.upload[0] == b"video bytes"
    assert blob.upload[1]["if_generation_match"] == 0

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
    assert captured["download"][0:2] == ("publisher-ingest", "folder/clip.mov")


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
