"""Reading the render worker's own record of a render."""
import datetime
from types import SimpleNamespace

import pytest
import requests

from app.services import vertical_render


class FakeDoc:
    def __init__(self, sequence):
        self.sequence = list(sequence)

    def get(self):
        data = self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]
        return SimpleNamespace(exists=data is not None, to_dict=lambda: dict(data or {}))


@pytest.fixture
def worker_doc(monkeypatch):
    sleeps = []
    monkeypatch.setattr(vertical_render, "_sleep", lambda seconds: sleeps.append(seconds))

    def install(*sequence):
        doc = FakeDoc(sequence)
        monkeypatch.setattr(vertical_render, "_render_doc", lambda video_id, render_id: doc)
        return sleeps

    return install


def http_error(status):
    return requests.HTTPError(response=SimpleNamespace(status_code=status))


def test_recoverable_errors_are_gateway_and_network_failures():
    for status in (502, 503, 504):
        assert vertical_render.is_recoverable(http_error(status))
    assert vertical_render.is_recoverable(requests.exceptions.ConnectionError())
    assert vertical_render.is_recoverable(requests.exceptions.ReadTimeout())
    for status in (400, 401, 500):
        assert not vertical_render.is_recoverable(http_error(status))
    assert not vertical_render.is_recoverable(ValueError("x"))


def test_worker_state(worker_doc):
    worker_doc({"status": "COMPLETED"})
    assert vertical_render.worker_state("v", "r") == "COMPLETED"
    worker_doc(None)
    assert vertical_render.worker_state("v", "r") == "MISSING"


def test_await_result_returns_the_stored_result(worker_doc):
    worker_doc({"status": "COMPLETED", "result": {"output_gcs_path": "o.mp4", "camera_plan_gcs_path": "plan"}})
    assert vertical_render.await_result("v", "r")["camera_plan_gcs_path"] == "plan"


def test_await_result_falls_back_to_the_output_path_for_older_renders(worker_doc):
    worker_doc({"status": "COMPLETED", "output_gcs_path": "gs://b/o.mp4"})
    assert vertical_render.await_result("v", "r") == {"status": "COMPLETED", "output_gcs_path": "gs://b/o.mp4"}


def test_await_result_waits_for_a_running_render(worker_doc):
    started = datetime.datetime.now(datetime.timezone.utc)
    sleeps = worker_doc(
        {"status": "PROCESSING", "started_at": started},
        {"status": "PROCESSING", "started_at": started},
        {"status": "COMPLETED", "result": {"output_gcs_path": "o.mp4"}},
    )
    assert vertical_render.await_result("v", "r", poll_seconds=7)["output_gcs_path"] == "o.mp4"
    assert sleeps == [7, 7]


def test_await_result_reports_worker_errors(worker_doc):
    worker_doc({"status": "ERROR", "error": "ffmpeg exploded"})
    with pytest.raises(vertical_render.RenderFailed, match="ffmpeg exploded"):
        vertical_render.await_result("v", "r")


def test_await_result_gives_up_when_the_worker_never_recorded_the_render(worker_doc, monkeypatch):
    worker_doc(None)
    clock = iter(range(0, 100_000, 100))
    monkeypatch.setattr(vertical_render.time, "monotonic", lambda: next(clock))
    with pytest.raises(vertical_render.RenderUnavailable, match="não registrou"):
        vertical_render.await_result("v", "r", appear_timeout=180)


def test_await_result_gives_up_on_a_lost_worker(worker_doc):
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=vertical_render.WORKER_MAX_RENDER_SECONDS + 60)
    worker_doc({"status": "PROCESSING", "started_at": long_ago})
    with pytest.raises(vertical_render.RenderUnavailable, match="não concluiu"):
        vertical_render.await_result("v", "r")


def test_await_result_has_an_absolute_cap_without_a_start_time(worker_doc, monkeypatch):
    worker_doc({"status": "PROCESSING"})
    clock = iter([0, 1, vertical_render.WORKER_MAX_RENDER_SECONDS + 10])
    monkeypatch.setattr(vertical_render.time, "monotonic", lambda: next(clock))
    with pytest.raises(vertical_render.RenderUnavailable, match="tempo máximo"):
        vertical_render.await_result("v", "r")


def test_await_result_reports_a_lost_render_by_its_stale_heartbeat(worker_doc):
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=vertical_render.WORKER_HEARTBEAT_STALE_SECONDS + 60)
    worker_doc({"status": "PROCESSING", "started_at": datetime.datetime.now(datetime.timezone.utc), "heartbeat_at": long_ago})
    with pytest.raises(vertical_render.RenderUnavailable, match="parou de reportar"):
        vertical_render.await_result("v", "r")


def test_await_result_keeps_waiting_on_a_fresh_heartbeat(worker_doc):
    now = datetime.datetime.now(datetime.timezone.utc)
    sleeps = worker_doc(
        {"status": "PROCESSING", "started_at": now, "heartbeat_at": now},
        {"status": "COMPLETED", "result": {"output_gcs_path": "o.mp4"}},
    )
    assert vertical_render.await_result("v", "r", poll_seconds=5)["output_gcs_path"] == "o.mp4"
    assert sleeps == [5]


def test_submit_does_not_resubmit_a_render_the_worker_already_has(monkeypatch):
    """A dropped connection mid-render must be adopted, not repeated."""
    from unittest.mock import patch, MagicMock
    from requests.exceptions import ConnectionError as RequestsConnectionError

    monkeypatch.setattr(vertical_render.settings, "VERTICAL_RENDER_URL", "https://render.example.com")
    monkeypatch.setattr(vertical_render.settings, "SECURITY_TOKEN", "secret")
    monkeypatch.setattr(vertical_render.time, "sleep", lambda _s: None)
    monkeypatch.setattr(vertical_render, "worker_state", lambda v, r: "PROCESSING")

    with patch.object(vertical_render.requests, "post", side_effect=RequestsConnectionError("dropped")) as mock_post:
        with pytest.raises(RequestsConnectionError):
            vertical_render.submit({"video_id": "v", "render_id": "r1"})

    assert mock_post.call_count == 1


def test_submit_resubmits_a_render_the_worker_never_received(monkeypatch):
    from unittest.mock import patch, MagicMock
    from requests.exceptions import ConnectionError as RequestsConnectionError

    monkeypatch.setattr(vertical_render.settings, "VERTICAL_RENDER_URL", "https://render.example.com")
    monkeypatch.setattr(vertical_render.settings, "SECURITY_TOKEN", "secret")
    monkeypatch.setattr(vertical_render.time, "sleep", lambda _s: None)
    monkeypatch.setattr(vertical_render, "worker_state", lambda v, r: "MISSING")

    good_response = MagicMock()
    good_response.raise_for_status.return_value = None
    good_response.json.return_value = {"status": "COMPLETED"}

    with patch.object(vertical_render.requests, "post", side_effect=[RequestsConnectionError("dropped"), good_response]) as mock_post:
        assert vertical_render.submit({"video_id": "v", "render_id": "r1"})["status"] == "COMPLETED"

    assert mock_post.call_count == 2


def test_submit_waits_for_a_free_instance_on_capacity_rejection(monkeypatch):
    from unittest.mock import patch, MagicMock

    monkeypatch.setattr(vertical_render.settings, "VERTICAL_RENDER_URL", "https://render.example.com")
    monkeypatch.setattr(vertical_render.settings, "SECURITY_TOKEN", "secret")
    sleeps = []
    monkeypatch.setattr(vertical_render, "_sleep", lambda s: sleeps.append(s))

    capacity = MagicMock()
    capacity.status_code = 503
    good_response = MagicMock()
    good_response.status_code = 200
    good_response.raise_for_status.return_value = None
    good_response.json.return_value = {"status": "COMPLETED"}

    with patch.object(vertical_render.requests, "post", side_effect=[capacity, good_response]) as mock_post:
        assert vertical_render.submit({"video_id": "v", "render_id": "r1"})["status"] == "COMPLETED"

    assert mock_post.call_count == 2
    assert sleeps == [vertical_render.INITIAL_WAIT_SECONDS]
