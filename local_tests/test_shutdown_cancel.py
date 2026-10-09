"""A shutdown cancel must not mark the archive failed (backport of upstream #5591)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from openviking.session.session import Session


@pytest.mark.parametrize("requested", [False, True])
def test_cancel_marks_archive_only_when_requested(requested):
    s = object.__new__(Session)
    s.session_id = "s"
    s.ctx = SimpleNamespace(account_id="default", user=SimpleNamespace(user_id="alice"))
    s._archives = SimpleNamespace(archive_index_from_uri=Mock(return_value=1))
    s._prepare_phase2_archive_messages = AsyncMock(side_effect=asyncio.CancelledError)
    s._write_failed_marker = AsyncMock()
    tracker = SimpleNamespace(is_cancellation_requested=Mock(return_value=requested))
    with patch("openviking.service.task_tracker.get_task_tracker", return_value=tracker):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(s._run_memory_extraction(
                task_id="t", archive_uri="viking://user/a/sessions/s/history/archive_001",
                messages=[], first_message_id="", last_message_id="", memory_policy=None))
    assert s._write_failed_marker.await_count == (1 if requested else 0)
