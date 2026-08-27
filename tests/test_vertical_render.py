"""Tests for the render dispatch retry logic."""
from unittest.mock import patch, MagicMock

import pytest
import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from app.services import vertical_render


def _make_connection_error(msg="Remote end closed connection without response"):
    return RequestsConnectionError(msg)


def test_submit_retries_on_transient_connection_error(monkeypatch):
    """A RemoteDisconnected should trigger a retry, not an immediate failure."""
    monkeypatch.setattr(vertical_render.settings, "VERTICAL_RENDER_URL", "https://render.example.com")
    monkeypatch.setattr(vertical_render.settings, "SECURITY_TOKEN", "secret")
    monkeypatch.setattr(vertical_render.time, "sleep", lambda _s: None)

    good_response = MagicMock()
    good_response.raise_for_status.return_value = None
    good_response.json.return_value = {"status": "COMPLETED", "output_gcs_path": "gs://bucket/out.mp4"}

    with patch.object(vertical_render.requests, "post", side_effect=[_make_connection_error(), good_response]) as mock_post:
        result = vertical_render.submit({"video_id": "v_test", "render_id": "r1"})

    assert result["status"] == "COMPLETED"
    assert mock_post.call_count == 2


def test_submit_raises_after_max_retries(monkeypatch):
    """Exhausting retries on transient errors should raise the last exception."""
    monkeypatch.setattr(vertical_render.settings, "VERTICAL_RENDER_URL", "https://render.example.com")
    monkeypatch.setattr(vertical_render.settings, "SECURITY_TOKEN", "secret")
    monkeypatch.setattr(vertical_render.time, "sleep", lambda _s: None)

    exc = _make_connection_error()
    with patch.object(vertical_render.requests, "post", side_effect=[exc, exc, exc]):
        with pytest.raises(RequestsConnectionError):
            vertical_render.submit({"video_id": "v_test", "render_id": "r1"})


def test_submit_does_not_retry_on_http_error(monkeypatch):
    """A 4xx/5xx HTTP response should not be retried — it's a worker-side failure."""
    monkeypatch.setattr(vertical_render.settings, "VERTICAL_RENDER_URL", "https://render.example.com")
    monkeypatch.setattr(vertical_render.settings, "SECURITY_TOKEN", "secret")

    bad_response = MagicMock()
    bad_response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
    bad_response.status_code = 500

    with patch.object(vertical_render.requests, "post", return_value=bad_response) as mock_post:
        with pytest.raises(requests.HTTPError):
            vertical_render.submit({"video_id": "v_test", "render_id": "r1"})

    assert mock_post.call_count == 1


def test_submit_raises_on_missing_url(monkeypatch):
    monkeypatch.setattr(vertical_render.settings, "VERTICAL_RENDER_URL", "")
    with pytest.raises(RuntimeError, match="VERTICAL_RENDER_URL"):
        vertical_render.submit({})
