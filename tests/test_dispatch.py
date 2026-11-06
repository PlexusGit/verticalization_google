"""Pipelines start on a Cloud Run Job, with an in-process fallback."""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.jobs import pipeline as job_pipeline
from app.services import dispatch, processing


class Tasks:
    def __init__(self):
        self.added = []

    def add_task(self, fn, *args):
        self.added.append((fn, args))


def fallback(*args):  # identity only; never called
    raise AssertionError("not executed in these tests")


@pytest.fixture
def job(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "PIPELINE_JOB_NAME", "plexus-pipeline")
    monkeypatch.setattr(settings, "PIPELINE_JOB_REGION", "us-central1")
    monkeypatch.setattr(settings, "PROJECT_ID", "proj")

    class Session:
        def post(self, url, json=None, timeout=None):
            calls.append((url, json))
            return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(dispatch, "_session", lambda: Session())
    return calls


def env_of(call):
    return {item["name"]: item["value"] for item in call[1]["overrides"]["containerOverrides"][0]["env"]}


def test_a_configured_job_is_started_with_the_run_parameters(job):
    tasks = Tasks()
    assert dispatch.start(tasks, "process", "v1", fallback, resume=True) == "job"
    assert tasks.added == []
    url = job[0][0]
    assert url == "https://run.googleapis.com/v2/projects/proj/locations/us-central1/jobs/plexus-pipeline:run"
    assert env_of(job[0]) == {
        "PIPELINE_KIND": "process", "PIPELINE_VIDEO_ID": "v1", "PIPELINE_RESUME": "1", "PIPELINE_CHAPTER_IDS": "",
    }


def test_finals_carry_their_chapter_ids(job):
    dispatch.start(Tasks(), "finals", "v1", fallback, chapter_ids=["chapter_001", "chapter_002"])
    env = env_of(job[0])
    assert env["PIPELINE_KIND"] == "finals" and env["PIPELINE_RESUME"] == "0"
    assert env["PIPELINE_CHAPTER_IDS"] == "chapter_001,chapter_002"


def test_without_a_job_the_pipeline_runs_in_process_with_the_original_arguments(monkeypatch):
    monkeypatch.setattr(settings, "PIPELINE_JOB_NAME", "")
    tasks = Tasks()
    assert dispatch.start(tasks, "process", "v1", fallback) == "in_process"
    assert dispatch.start(tasks, "process", "v1", fallback, resume=True) == "in_process"
    assert dispatch.start(tasks, "finals", "v1", fallback, chapter_ids=["c1"]) == "in_process"
    assert dispatch.start(tasks, "finals", "v1", fallback, chapter_ids=["c1"], resume=True) == "in_process"
    assert [args for _fn, args in tasks.added] == [("v1",), ("v1", True), ("v1", ["c1"]), ("v1", ["c1"], True)]


def test_a_job_that_cannot_start_falls_back_to_in_process(job, monkeypatch):
    class Broken:
        def post(self, *a, **k):
            raise RuntimeError("PERMISSION_DENIED")

    monkeypatch.setattr(dispatch, "_session", lambda: Broken())
    tasks = Tasks()
    assert dispatch.start(tasks, "process", "v1", fallback, resume=True) == "in_process"
    assert tasks.added == [(fallback, ("v1", True))]


def test_an_error_response_from_the_jobs_api_falls_back(job, monkeypatch):
    class Denied:
        def post(self, *a, **k):
            def boom():
                raise RuntimeError("403")
            return SimpleNamespace(raise_for_status=boom)

    monkeypatch.setattr(dispatch, "_session", lambda: Denied())
    tasks = Tasks()
    assert dispatch.start(tasks, "process", "v1", fallback) == "in_process"


def test_the_job_entrypoint_runs_the_requested_pipeline(monkeypatch):
    ran = []
    monkeypatch.setattr(processing, "process_video", lambda vid, resume: ran.append(("process", vid, resume)))
    monkeypatch.setattr(processing, "render_final_chapters", lambda vid, ids, resume: ran.append(("finals", vid, ids, resume)))

    assert job_pipeline.main({"PIPELINE_KIND": "process", "PIPELINE_VIDEO_ID": "v1", "PIPELINE_RESUME": "1"}) == 0
    assert job_pipeline.main({"PIPELINE_KIND": "finals", "PIPELINE_VIDEO_ID": "v2", "PIPELINE_CHAPTER_IDS": "a, b,,c"}) == 0
    assert ran == [("process", "v1", True), ("finals", "v2", ["a", "b", "c"], False)]


def test_the_job_entrypoint_rejects_bad_input(monkeypatch):
    monkeypatch.setattr(processing, "process_video", lambda *a: pytest.fail("must not run"))
    assert job_pipeline.main({"PIPELINE_KIND": "process"}) == 2
    assert job_pipeline.main({"PIPELINE_KIND": "nope", "PIPELINE_VIDEO_ID": "v1"}) == 2
