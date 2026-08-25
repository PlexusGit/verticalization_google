from app.models import VideoStatus
from app.routers.api import _approval_block_reason


def test_approval_is_only_open_during_pending_review():
    assert _approval_block_reason({"status": VideoStatus.TRANSCRIBING.value})
    assert _approval_block_reason({"status": VideoStatus.SUGGESTING.value})
    assert _approval_block_reason({"status": VideoStatus.PENDING_REVIEW.value}) is None


def test_approval_is_locked_once_persisted_or_completed():
    assert _approval_block_reason({"status": VideoStatus.PENDING_REVIEW.value, "approved_at": "now"})
    assert _approval_block_reason({"status": VideoStatus.RENDERING_FINALS.value})
    assert _approval_block_reason({"status": VideoStatus.COMPLETED.value})
