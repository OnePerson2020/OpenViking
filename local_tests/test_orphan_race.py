"""2026-10-08: a predecessor that completes during the orphan check is not an orphan."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from openviking.session.session import Session


def _session():
    s = Session.__new__(Session)
    s._session_uri = "viking://user/u/sessions/s1"
    s._viking_fs = MagicMock()
    s._viking_fs.exists = AsyncMock(return_value=True)
    s._viking_fs.write_file = AsyncMock()
    s._archives = MagicMock()
    s.ctx = MagicMock()
    s._read_phase1_meta = AsyncMock(
        return_value={"status": "ready", "queue_message": {"task_id": "t-1"}}
    )
    s._write_failed_marker = AsyncMock()
    return s


class OrphanRaceTests(IsolatedAsyncioTestCase):
    async def _run(self, states):
        s = _session()
        s._archives.terminal_state = AsyncMock(side_effect=states)
        tracker = MagicMock(has_work=MagicMock(return_value=False), fail=AsyncMock())
        with patch("openviking.service.task_tracker.get_task_tracker", return_value=tracker):
            ok = await s._can_run_archive(5)
        return ok, s, tracker

    async def test_completion_between_checks_is_not_marked_failed(self):
        ok, s, tracker = await self._run(["pending", "completed"])
        self.assertTrue(ok)
        s._write_failed_marker.assert_not_called()
        tracker.fail.assert_not_called()

    async def test_true_orphan_is_still_marked_failed(self):
        ok, s, tracker = await self._run(["pending", "pending"])
        self.assertTrue(ok)
        s._write_failed_marker.assert_awaited_once()
        self.assertEqual(s._write_failed_marker.await_args.kwargs["stage"], "queue_missing")
        tracker.fail.assert_awaited_once()


class FailedMarkerGuardTests(IsolatedAsyncioTestCase):
    async def _write(self, done_exists):
        s = Session.__new__(Session)
        s._viking_fs = MagicMock(write_file=AsyncMock())
        s._archives = MagicMock(file_exists=AsyncMock(return_value=done_exists))
        s.ctx = MagicMock()
        await s._write_failed_marker("viking://a/archive_002", stage="queue_missing", error="x")
        return s._viking_fs.write_file

    async def test_completed_archive_gets_no_failure_marker(self):
        (await self._write(True)).assert_not_called()

    async def test_unfinished_archive_gets_failure_marker(self):
        w = await self._write(False)
        w.assert_awaited_once()
        self.assertTrue(w.await_args.kwargs["uri"].endswith("/.failed.json"))
