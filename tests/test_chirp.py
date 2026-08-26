from types import SimpleNamespace

import pytest
from google.api_core import exceptions as google_exceptions
from google.protobuf import duration_pb2
from google.rpc import error_details_pb2

from app.services import chirp


def test_request_explicitly_uses_chirp_3_and_speech_v2():
    request = chirp._request("us", "gs://bucket/audio.flac")
    assert request.config.model == "chirp_3"
    assert request.config.language_codes == ["pt-BR"]
    assert request.recognizer.endswith("/locations/us/recognizers/_")
    assert request.config.features.enable_word_time_offsets is True


def test_resource_exhausted_details_include_reason_quota_and_server_delay():
    error = google_exceptions.ResourceExhausted(
        "capacity",
        details=[
            error_details_pb2.ErrorInfo(
                reason="RATE_LIMIT_EXCEEDED",
                domain="googleapis.com",
                metadata={"quota_metric": "speech.googleapis.com/operation_requests"},
            ),
            error_details_pb2.QuotaFailure(
                violations=[error_details_pb2.QuotaFailure.Violation(subject="project:example", description="operation limit")]
            ),
            error_details_pb2.RetryInfo(retry_delay=duration_pb2.Duration(seconds=7)),
        ],
    )
    details = chirp._error_details(error)
    assert details["reason"] == "RATE_LIMIT_EXCEEDED"
    assert details["metadata"]["quota_metric"] == "speech.googleapis.com/operation_requests"
    assert details["quota_violations"][0]["description"] == "operation limit"
    assert details["server_retry_delay_seconds"] == 7


def test_resource_exhausted_alternates_region_and_logs_operation(monkeypatch):
    response = object()
    calls = []
    sleeps = []
    progress = []

    class Operation:
        def __init__(self, location):
            self.location = location
            self.operation = SimpleNamespace(name=f"operations/{location}")

        def result(self, timeout):
            assert timeout == 50 * 60
            if self.location == "us":
                raise google_exceptions.ResourceExhausted("temporary capacity")
            return response

    class Client:
        def __init__(self, location):
            self.location = location

        def batch_recognize(self, request):
            assert request.config.model == "chirp_3"
            calls.append(self.location)
            return Operation(self.location)

    monkeypatch.setattr(chirp.settings, "CHIRP_LOCATION", "us")
    monkeypatch.setattr(chirp.settings, "CHIRP_FALLBACK_LOCATION", "eu")
    monkeypatch.setattr(chirp, "_client", lambda location: Client(location))
    monkeypatch.setattr(chirp.random, "uniform", lambda _start, _end: 1)
    monkeypatch.setattr(chirp.time, "sleep", sleeps.append)

    result = chirp._recognize("gs://bucket/audio.flac", lambda *args: progress.append(args))

    assert result is response
    assert calls == ["us", "eu"]
    assert sleeps == [5]
    retry = next(details for action, _message, details in progress if action == "chirp_resource_exhausted")
    assert retry["operation_name"] == "operations/us"
    assert retry["will_retry"] is True
    completed = next(details for action, _message, details in progress if action == "chirp_request_completed")
    assert completed["operation_name"] == "operations/eu"


def test_final_resource_exhausted_emits_failure_event(monkeypatch):
    progress = []

    class Operation:
        operation = SimpleNamespace(name="operations/failed")

        def result(self, timeout):
            raise google_exceptions.ResourceExhausted("temporary capacity")

    client = SimpleNamespace(batch_recognize=lambda request: Operation())
    monkeypatch.setattr(chirp.settings, "CHIRP_LOCATION", "us")
    monkeypatch.setattr(chirp.settings, "CHIRP_FALLBACK_LOCATION", "eu")
    monkeypatch.setattr(chirp, "_client", lambda _location: client)
    monkeypatch.setattr(chirp.random, "uniform", lambda _start, _end: 1)
    monkeypatch.setattr(chirp.time, "sleep", lambda _delay: None)

    with pytest.raises(google_exceptions.ResourceExhausted):
        chirp._recognize("gs://bucket/audio.flac", lambda *args: progress.append(args))

    attempts = [details for action, _message, details in progress if action == "chirp_request_started"]
    assert [details["location"] for details in attempts] == ["us", "eu", "us", "eu", "us", "eu"]
    assert progress[-1][0] == "chirp_request_failed"
    assert progress[-1][2]["attempt_count"] == 6
