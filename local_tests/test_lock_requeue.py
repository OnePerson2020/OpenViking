"""Lock contention requeues session commits instead of failing archives (2026-10-09)."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from openviking.message import Message, TextPart
from openviking.pyagfs.async_client import AsyncAGFSClient
from openviking.session.session import Session
from openviking.storage.errors import LockAcquisitionError
from openviking.storage.queuefs.session_commit_msg import SessionCommitMsg
from openviking.storage.queuefs.session_commit_processor import SessionCommitProcessor
from openviking_cli.exceptions import NotFoundError

ARCHIVE = "viking://user/alice/sessions/s/history/archive_002"


def _msg():
    return SessionCommitMsg(task_id="t", session_id="s", session_uri="viking://user/alice/sessions/s",
                            archive_uri=ARCHIVE, user={"account_id": "default", "user_id": "alice"})


def _session():
    s = object.__new__(Session)
    s.session_id = "s"
    s.ctx = SimpleNamespace(account_id="default", user=SimpleNamespace(user_id="alice"))
    s._viking_fs = SimpleNamespace(read_file=AsyncMock(side_effect=NotFoundError("x", "file")))
    s._archives = SimpleNamespace(
        archive_index_from_uri=Mock(return_value=2),
        read_messages=AsyncMock(return_value=[Message(id="m1", role="user", parts=[TextPart(text="hi")])]),
        read_meta=AsyncMock(return_value={}),
    )
    s._activate_archive_recovery = AsyncMock()
    s._ensure_phase1_ready = AsyncMock(return_value=True)
    s._can_run_archive = AsyncMock(return_value=True)
    s._write_failed_marker = AsyncMock()
    return s


def _tracker():
    return SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(value="running"))),
                           fail=AsyncMock(), complete=AsyncMock(), start=AsyncMock())


def test_polled_timeout_reports_real_wait_and_path():
    client = object.__new__(AsyncAGFSClient)
    client.run = AsyncMock(side_effect=LockAcquisitionError("lock acquire timed out after 0ms"))
    with pytest.raises(LockAcquisitionError, match=r"after 100ms \(polled\): /local/s"):
        asyncio.run(client.pathlock_acquire_exact("/local/s", timeout_secs=0.1))


def test_resume_requeues_on_lock_contention():
    s, tracker = _session(), _tracker()
    s._run_memory_extraction = AsyncMock(side_effect=LockAcquisitionError("busy"))
    with patch("openviking.service.task_tracker.get_task_tracker", return_value=tracker):
        assert asyncio.run(s.resume_queued_commit(_msg())) is False
    tracker.fail.assert_not_awaited()


def test_phase2_lock_error_writes_no_failed_marker():
    s, tracker = _session(), _tracker()
    s._prepare_phase2_archive_messages = AsyncMock(side_effect=LockAcquisitionError("busy"))
    with patch("openviking.service.task_tracker.get_task_tracker", return_value=tracker):
        with pytest.raises(LockAcquisitionError):
            asyncio.run(s._run_memory_extraction(
                task_id="t", archive_uri=ARCHIVE, messages=[], first_message_id="m1",
                last_message_id="m1", memory_policy=None))
    s._write_failed_marker.assert_not_awaited()
    tracker.fail.assert_not_awaited()


def test_processor_requeues_when_session_load_is_locked():
    session = SimpleNamespace(exists=AsyncMock(return_value=True),
                              load=AsyncMock(side_effect=LockAcquisitionError("busy")),
                              resume_queued_commit=AsyncMock())
    proc = SessionCommitProcessor(SimpleNamespace(session=Mock(return_value=session)))
    qm = SimpleNamespace(enqueue=AsyncMock())
    with patch("openviking.storage.queuefs.get_queue_manager", return_value=qm):
        result = asyncio.run(proc.on_dequeue({"data": json.dumps(_msg().to_dict())}))
    session.resume_queued_commit.assert_not_awaited()
    qm.enqueue.assert_awaited_once()
    assert result.outcome.value == "requeued"
