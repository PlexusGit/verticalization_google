"""The transcode client waits for a free worker instead of failing the video."""
from types import SimpleNamespace

import pytest
import requests

from app.config import settings
from app.services import transcode


def response(status, text="", payload=None):
    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"{status} Server Error", response=SimpleNamespace(status_code=status))

    return SimpleNamespace(status_code=status, text=text, raise_for_status=raise_for_status, json=lambda: payload or {})


CAPACITY_TEXT = "The request was aborted after waiting too long for an available instance."


@pytest.fixture
def client(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(settings, "TRANSCODE_URL", "https://worker.example")
    monkeypatch.setattr(settings, "SECURITY_TOKEN", "k")
    monkeypatch.setattr(settings, "TRANSCODE_QUEUE_WAIT_SECONDS", 1800)
    monkeypatch.setattr(transcode, "_sleep", sleeps.append)

    def install(*responses):
        queue = list(responses)

        def post(url, **kwargs):
            calls.append(url)
            return queue.pop(0) if len(queue) > 1 else queue[0]

        monkeypatch.setattr(transcode.requests, "post", post)
        return sleeps, calls

    return install


def test_the_exact_failure_is_retried_until_the_worker_is_free(client):
    """Cloud Run's 500 'waiting too long for an available instance' while another video transcodes."""

    sleeps, calls = client(response(500, CAPACITY_TEXT), response(500, CAPACITY_TEXT), response(200, payload={"ok": 1}))
    assert transcode.submit("v1", "v1/src.mp4") == {"ok": 1}
    assert len(calls) == 3
    assert sleeps == [15, 30]


@pytest.mark.parametrize("status", [429, 503])
def test_capacity_statuses_are_retried(client, status):
    sleeps, calls = client(response(status), response(200, payload={"ok": 1}))
    assert transcode.submit("v1", "v1/src.mp4") == {"ok": 1}
    assert len(calls) == 2


def test_waits_back_off_to_a_ceiling(client):
    sleeps, _ = client(*[response(500, CAPACITY_TEXT)] * 5, response(200, payload={}))
    transcode.submit("v1", "v1/src.mp4")
    assert sleeps == [15, 30, 60, 60, 60]


def test_a_real_worker_failure_is_never_retried(client):
    sleeps, calls = client(response(500, '{"error": "internal transcode error"}'))
    with pytest.raises(requests.HTTPError):
        transcode.submit("v1", "v1/src.mp4")
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_client_errors_are_never_retried(client, status):
    _, calls = client(response(status))
    with pytest.raises(requests.HTTPError):
        transcode.submit("v1", "v1/src.mp4")
    assert len(calls) == 1


def test_gives_up_after_the_configured_wait(client, monkeypatch):
    monkeypatch.setattr(settings, "TRANSCODE_QUEUE_WAIT_SECONDS", 100)
    clock = iter(range(0, 10_000, 40))
    monkeypatch.setattr(transcode.time, "monotonic", lambda: next(clock))
    sleeps, calls = client(response(500, CAPACITY_TEXT))
    with pytest.raises(requests.HTTPError):
        transcode.submit("v1", "v1/src.mp4")
    assert 1 < len(calls) < 6


def test_requires_a_configured_url(monkeypatch):
    monkeypatch.setattr(settings, "TRANSCODE_URL", "")
    with pytest.raises(RuntimeError, match="TRANSCODE_URL"):
        transcode.submit("v1", "v1/src.mp4")
