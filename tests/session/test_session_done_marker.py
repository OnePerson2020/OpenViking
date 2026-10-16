"""Session commit writes .done only after the task result is saved (upstream #6153)."""

from unittest.mock import AsyncMock

from openviking.service.task_tracker import get_task_tracker
from openviking.session import Session
from tests.session.test_session_commit import _marker_exists, _wait_for_task


async def test_task_result_failure_leaves_no_done_marker(
    wm_session_with_messages: Session, monkeypatch
):
    """.done is written after the task result is saved: a failed save must not
    leave a completed archive behind a failed task (upstream #6153)."""
    session = wm_session_with_messages
    session._session_compressor.extract_long_term_memories = AsyncMock(return_value=[])
    tracker = get_task_tracker()

    async def failing_complete(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("synthetic task result failure")

    monkeypatch.setattr(tracker, "complete", failing_complete)
    # Offline embeddings never drain the request queues; do not let Phase 2 wait on them.
    monkeypatch.setattr("openviking.session.session._PHASE2_QUEUE_WAIT_TIMEOUT_SECONDS", 1.0)
    result = await session.commit_async()
    task_result = await _wait_for_task(result["task_id"])

    assert task_result["status"] == "failed"
    assert not await _marker_exists(session, result["archive_uri"], ".done")
