"""Waiting and terminal archive deliveries must not reload the live conversation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_previous_safety import FakeAgfs, RecoveryFS

from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskTracker
from openviking.session.session import Session
from openviking.storage.queuefs.session_commit_msg import SessionCommitMsg
from openviking.storage.queuefs.session_commit_processor import SessionCommitProcessor


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["waiting", "done", "ready"])
async def test_queue_loads_live_session_only_when_archive_can_run(monkeypatch, state):
    uri = "viking://user/alice/sessions/s"
    archive = uri + "/history/archive_002"
    fs = RecoveryFS()
    fs.files[uri + "/.meta.json"] = "{}"
    session = Session(viking_fs=fs, session_id="s", session_uri=uri)
    session.exists = AsyncMock(return_value=True)
    session.load = AsyncMock()
    session._ensure_phase1_ready = AsyncMock(return_value=True)
    session._can_run_archive = AsyncMock(return_value=state == "ready")
    session._archives.read_messages = AsyncMock(return_value=[SimpleNamespace(id="m")])
    session._archives.read_meta = AsyncMock(return_value={})
    session._run_memory_extraction = AsyncMock()
    if state == "done":
        fs.files[archive + "/.done"] = "{}"
    tracker = TaskTracker(PersistentTaskStore(FakeAgfs()))
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr(
        "openviking.storage.queuefs.session_commit_processor.get_task_tracker", lambda: tracker
    )
    monkeypatch.setattr(
        "openviking.storage.queuefs.get_queue_manager", lambda: SimpleNamespace(enqueue=AsyncMock())
    )
    monkeypatch.setattr(
        "openviking.storage.queuefs.session_commit_processor.asyncio.sleep", AsyncMock()
    )
    processor = SessionCommitProcessor(SimpleNamespace(session=lambda *a, **kw: session))
    msg = SessionCommitMsg(
        task_id="t",
        session_id="s",
        session_uri=uri,
        archive_uri=archive,
        user={"account_id": "default", "user_id": "alice"},
    )
    result = await processor.on_dequeue({"data": json.dumps(msg.to_dict())})
    assert session.load.await_count == (1 if state == "ready" else 0)
    assert session._run_memory_extraction.await_count == (1 if state == "ready" else 0)
    assert result.outcome.value == ("requeued" if state == "waiting" else "success")
