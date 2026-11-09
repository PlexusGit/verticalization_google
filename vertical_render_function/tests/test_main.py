from pathlib import Path
from unittest.mock import Mock

from flask import Flask, request
import pytest

import main
from renderer import MAX_EXPRESSION_KEYFRAMES


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

    def fake_render(spec, input_path, output_path, **_kwargs):
        assert Path(input_path).exists()
        assert spec.automatic_focal_points == ({"time": 0.0, "x": 800.0, "y": 500.0},)
        Path(output_path).write_bytes(b"mp4")

    monkeypatch.setattr(main, "_download", download)
    monkeypatch.setattr(main, "analyze_focal_points", lambda *_args: ({"time": 0.0, "x": 800.0, "y": 500.0},))
    monkeypatch.setattr(main, "render", fake_render)
    monkeypatch.setattr(main, "_upload", lambda uri, source: "gs://output/render.mp4" if Path(source).exists() else None)

    body, status, _ = invoke(app, json=valid_payload(), headers={"X-API-Key": "key"})
    assert status == 200 and body["width"] == 1080 and body["height"] == 1920
    assert body["automatic_focal_points"] == [{"time": 0.0, "x": 800.0, "y": 500.0}]
    assert [entry[0][3] for entry in updates] == ["PROCESSING", "COMPLETED"]
    assert all(entry[0][2] == "chapter_1" for entry in updates)
    # The full response is stored with the status so a caller that lost the
    # HTTP response can still adopt the render.
    assert updates[-1][1]["render_only"] == {"result": body}


def test_update_status_stores_the_result_on_the_render_document_only(monkeypatch):
    batch = Mock()
    docs = {}

    class Doc:
        def __init__(self, path):
            self.path = path

        def collection(self, name):
            return Doc(self.path + "/" + name)

        def document(self, name):
            return Doc(self.path + "/" + name)

    db = Mock()
    db.collection.side_effect = lambda name: Doc(name)
    db.batch.return_value = batch
    monkeypatch.setattr(main, "firestore_client", lambda: db)

    main.update_status("v1", "r1", "c1", "COMPLETED", render_only={"result": {"x": 1}}, output_gcs_path="o")

    (render_call, chapter_call) = batch.set.call_args_list
    assert render_call.args[0].path.endswith("/renders/r1")
    assert render_call.args[1]["result"] == {"x": 1} and render_call.args[1]["output_gcs_path"] == "o"
    assert chapter_call.args[0].path.endswith("/chapters/c1")
    assert "result" not in chapter_call.args[1] and chapter_call.args[1]["output_gcs_path"] == "o"


def test_automatic_focus_falls_back_to_center_on_detection_error(monkeypatch):
    spec = main.validate_request(valid_payload())
    monkeypatch.setattr(main, "analyze_focal_points", Mock(side_effect=RuntimeError("detector")))
    assert main._add_automatic_focus(spec, "/tmp/source") is spec


def test_manual_mode_skips_face_detection(monkeypatch):
    spec = main.validate_request({**valid_payload(), "mode": "split_stack"})
    analyze = Mock()
    monkeypatch.setattr(main, "analyze_focal_points", analyze)
    assert main._add_automatic_focus(spec, "/tmp/source") is spec
    analyze.assert_not_called()


def test_cached_points_are_compacted_without_face_detection(monkeypatch):
    points = [{"time": index, "x": 800 + index, "y": 500} for index in range(100)]
    spec = main.validate_request({**valid_payload(), "automatic_focal_points": points})
    analyze = Mock()
    monkeypatch.setattr(main, "analyze_focal_points", analyze)
    result = main._add_automatic_focus(spec, "/tmp/source")
    assert len(result.automatic_focal_points) == MAX_EXPRESSION_KEYFRAMES
    analyze.assert_not_called()


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


def test_heartbeat_loop_updates_only_the_render_heartbeat(monkeypatch):
    writes = []

    class Doc:
        def __init__(self, path):
            self.path = path

        def collection(self, name):
            return Doc(self.path + "/" + name)

        def document(self, name):
            return Doc(self.path + "/" + name)

        def set(self, data, merge=False):
            writes.append((self.path, data, merge))

    db = Mock()
    db.collection.side_effect = lambda name: Doc(name)
    monkeypatch.setattr(main, "firestore_client", lambda: db)

    stop = Mock()
    stop.wait.side_effect = [False, True]
    main._heartbeat_loop("v1", "r1", stop)

    assert len(writes) == 1
    path, data, merge = writes[0]
    assert path.endswith("/renders/r1") and merge is True
    assert "heartbeat_at" in data and "status" not in data


def test_render_starts_and_stops_the_heartbeat(app, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "key")
    stops = []
    started = []

    class FakeStop:
        def set(self):
            stops.append(True)

    monkeypatch.setattr(main.threading, "Event", lambda: FakeStop())

    class FakeThread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            pass

    monkeypatch.setattr(main.threading, "Thread", lambda **kw: FakeThread(**kw))
    monkeypatch.setattr(main, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(main, "_download", lambda path, dest: "1")
    monkeypatch.setattr(main, "_upload", lambda path, src: "gs://b/out.mp4")
    monkeypatch.setattr(main, "render", lambda *a, **k: None)

    body = valid_payload()
    body["mode"] = "split_stack"
    body["keyframes_top"] = [{"time": 0, "x": 0, "y": 0}]
    body["keyframes_bottom"] = [{"time": 0, "x": 0, "y": 540}]
    response, status, _headers = invoke(app, json=body, headers={"X-API-Key": "key"})
    assert status == 200
    (target, args, daemon) = started[0]
    assert target == main._heartbeat_loop and args[0] == "video_1" and args[1] == "render_1" and daemon
    assert stops == [True]


def _horizontal_setup(monkeypatch, *, horizontal):
    monkeypatch.setattr(main, "API_KEY", "key")
    monkeypatch.setattr(main, "update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_download", lambda _uri, destination: Path(destination).write_bytes(b"source"))
    monkeypatch.setattr(main, "analyze_focal_points", lambda *_args: ())
    monkeypatch.setattr(main, "render", lambda spec, input_path, output_path, **_kw: Path(output_path).write_bytes(b"mp4"))
    uploads = []
    monkeypatch.setattr(main, "_upload", lambda uri, source: uploads.append(uri) or f"gs://bucket/{uri}")
    cuts = []

    def fake_horizontal(spec, input_path, output_path, **_kw):
        assert Path(input_path).exists()
        cuts.append(spec.horizontal_output_gcs_path)
        if horizontal == "fail":
            raise RuntimeError("ffmpeg horizontal cut failed: boom")
        Path(output_path).write_bytes(b"horizontal")

    monkeypatch.setattr(main, "render_horizontal", fake_horizontal)
    return uploads, cuts


def test_final_render_also_produces_the_horizontal_cut_from_the_same_source(app, monkeypatch):
    uploads, cuts = _horizontal_setup(monkeypatch, horizontal="ok")
    payload = {**valid_payload(), "horizontal_output_gcs_path": "video_1/chapters/chapter_1/horizontal_render_1.mp4"}

    body, status, _ = invoke(app, json=payload, headers={"X-API-Key": "key"})

    assert status == 200 and body["status"] == "COMPLETED"
    assert cuts == ["video_1/chapters/chapter_1/horizontal_render_1.mp4"]
    assert body["horizontal_gcs_path"] == "gs://bucket/video_1/chapters/chapter_1/horizontal_render_1.mp4"
    assert "horizontal_error" not in body and len(uploads) == 2


def test_horizontal_failure_never_fails_the_vertical_render(app, monkeypatch):
    uploads, cuts = _horizontal_setup(monkeypatch, horizontal="fail")
    payload = {**valid_payload(), "horizontal_output_gcs_path": "video_1/chapters/chapter_1/horizontal_render_1.mp4"}

    body, status, _ = invoke(app, json=payload, headers={"X-API-Key": "key"})

    assert status == 200 and body["status"] == "COMPLETED" and body["output_gcs_path"]
    assert "boom" in body["horizontal_error"] and "horizontal_gcs_path" not in body
    assert len(uploads) == 1  # only the vertical output was uploaded


def test_drafts_without_a_horizontal_path_skip_the_cut(app, monkeypatch):
    _uploads, cuts = _horizontal_setup(monkeypatch, horizontal="ok")

    body, status, _ = invoke(app, json=valid_payload(), headers={"X-API-Key": "key"})

    assert status == 200 and cuts == []
    assert "horizontal_gcs_path" not in body and "horizontal_error" not in body
