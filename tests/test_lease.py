"""Heartbeat lease: stale detection, atomic claim and heartbeat lifecycle."""
import datetime
import threading
from types import SimpleNamespace

import pytest
from google.cloud import firestore

from app.services import lease

NOW = datetime.datetime(2026, 11, 5, 21, 0, tzinfo=datetime.timezone.utc)


def ago(seconds):
    return NOW - datetime.timedelta(seconds=seconds)


def test_only_working_statuses_can_be_stale():
    for status in ("PENDING_REVIEW", "COMPLETED", "ERROR", "UPLOADING"):
        assert not lease.is_stale({"status": status, "updated_at": ago(10_000)}, now=NOW)
    for status in lease.WORKING_STATUSES:
        assert lease.is_stale({"status": status, "updated_at": ago(10_000)}, now=NOW)


def test_fresh_heartbeat_or_recent_write_keeps_a_video_alive():
    base = {"status": "RENDERING_DRAFTS"}
    assert not lease.is_stale({**base, "heartbeat_at": ago(60), "updated_at": ago(10_000)}, now=NOW)
    assert not lease.is_stale({**base, "updated_at": ago(60)}, now=NOW)
    assert lease.is_stale({**base, "heartbeat_at": ago(lease.LEASE_SECONDS + 5), "updated_at": ago(10_000)}, now=NOW)
    assert lease.is_stale(base, now=NOW)


def test_naive_timestamps_are_treated_as_utc():
    naive = (NOW - datetime.timedelta(seconds=10)).replace(tzinfo=None)
    assert not lease.is_stale({"status": "SUGGESTING", "heartbeat_at": naive}, now=NOW)


def test_a_pipeline_running_in_this_process_is_never_stale():
    with lease._active_lock:
        lease._active.add("v1")
    try:
        assert not lease.is_stale({"status": "RENDERING_DRAFTS", "updated_at": ago(10_000)}, "v1", NOW)
        assert lease.is_stale({"status": "RENDERING_DRAFTS", "updated_at": ago(10_000)}, "other", NOW)
    finally:
        with lease._active_lock:
            lease._active.discard("v1")


def test_decide_claim_resumes_gives_up_or_skips():
    stale = {"status": "RENDERING_DRAFTS", "updated_at": ago(10_000)}
    action, fields = lease.decide_claim(stale, "v1", NOW)
    assert action == "resume"
    assert fields["resume_count"] == 1 and fields["lease_owner"] == lease.INSTANCE_ID

    action, fields = lease.decide_claim({**stale, "resume_count": lease.MAX_RESUMES}, "v1", NOW)
    assert action == "give_up"
    assert fields["status"] == "ERROR" and fields["error_type"] == "PipelineInterrupted"

    assert lease.decide_claim({"status": "RENDERING_DRAFTS", "heartbeat_at": ago(5)}, "v1", NOW) == ("skip", {})


class FakeSnapshot:
    def __init__(self, data):
        self._data, self.exists = data, data is not None

    def to_dict(self):
        return dict(self._data)


class FakeRef:
    def __init__(self, data):
        self.data = data
        self.updates = []

    def get(self, transaction=None):
        return FakeSnapshot(self.data)

    def update(self, fields):
        self.updates.append(fields)


class FakeDb:
    def __init__(self, ref):
        self.ref = ref

    def collection(self, _name):
        return self

    def document(self, _id):
        return self.ref

    def transaction(self):
        ref = self.ref
        return SimpleNamespace(update=lambda target, fields: ref.updates.append(fields))


@pytest.fixture
def claim_env(monkeypatch):
    monkeypatch.setattr(lease.firestore, "transactional", lambda fn: fn)

    def install(data):
        ref = FakeRef(data)
        monkeypatch.setattr(lease, "get_db", lambda: FakeDb(ref))
        return ref

    return install


def test_claim_resume_writes_the_claim_atomically(claim_env):
    ref = claim_env({"status": "RENDERING_FINALS", "updated_at": ago(10_000)})
    assert lease.claim_resume("v1") == "resume"
    assert ref.updates[0]["resume_count"] == 1


def test_claim_resume_loses_when_someone_else_already_claimed(claim_env):
    ref = claim_env({"status": "RENDERING_FINALS", "heartbeat_at": datetime.datetime.now(datetime.timezone.utc)})
    assert lease.claim_resume("v1") == "skip"
    assert ref.updates == []


def test_claim_resume_on_a_missing_video(claim_env):
    claim_env(None)
    assert lease.claim_resume("v1") == "skip"


def test_pipeline_lease_beats_while_running_and_releases(monkeypatch):
    ref = FakeRef({})
    monkeypatch.setattr(lease, "get_db", lambda: FakeDb(ref))
    monkeypatch.setattr(lease, "HEARTBEAT_SECONDS", 0.01)
    beats = threading.Event()
    original = ref.update
    ref.update = lambda fields: (original(fields), beats.set() if len(ref.updates) >= 3 else None)[0]

    with lease.pipeline_lease("v1"):
        assert lease.is_active_here("v1")
        assert beats.wait(2), "heartbeat was not refreshed while the block ran"
    assert not lease.is_active_here("v1")
    assert ref.updates[0]["lease_owner"] == lease.INSTANCE_ID
    assert "heartbeat_at" in ref.updates[0]
    assert ref.updates[-1] == {"lease_owner": firestore.DELETE_FIELD}


def test_pipeline_lease_is_released_when_the_pipeline_raises(monkeypatch):
    ref = FakeRef({})
    monkeypatch.setattr(lease, "get_db", lambda: FakeDb(ref))
    with pytest.raises(RuntimeError):
        with lease.pipeline_lease("v2"):
            raise RuntimeError("boom")
    assert not lease.is_active_here("v2")
