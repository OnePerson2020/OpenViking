"""2026-10-08: auto-captured agent artifacts never create reason commits, and other
reason commits are queued without holding the caller until they finish."""
import asyncio

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from openviking.service.resource_memory_link_service import ResourceMemoryLinkService


class ReasonSkipTests(IsolatedAsyncioTestCase):
    async def test_agent_artifact_reason_is_skipped_without_touching_sessions(self):
        sessions = MagicMock()
        sessions.get = AsyncMock()
        sessions.commit_async = AsyncMock()
        service = ResourceMemoryLinkService(session_service=sessions)
        result = await service.on_resource_added(
            ctx=MagicMock(),
            resource_uri="viking://resources/agent-artifacts/pi/pi-1/abc-file.py",
            reason="pi session pi-1 file /tmp/file.py",
        )
        self.assertEqual(result, {"status": "skipped", "reason": "auto_captured_artifact"})
        sessions.get.assert_not_called()
        sessions.commit_async.assert_not_called()

    async def test_other_resources_still_reach_the_reason_flow(self):
        sessions = MagicMock()
        sessions.get = AsyncMock(side_effect=RuntimeError("reached session flow"))
        service = ResourceMemoryLinkService(session_service=sessions)
        service._read_resource_directory_abstract = AsyncMock(return_value="")
        with patch("openviking.service.resource_memory_link_service._resource_reason_peer_id",
                   return_value=None), self.assertRaisesRegex(RuntimeError, "reached session flow"):
            await service.on_resource_added(
                ctx=MagicMock(), resource_uri="viking://resources/report.pdf", reason="user reason")

    async def test_reason_commit_is_queued_without_waiting_for_it(self):
        session = MagicMock()
        session.meta = MagicMock()
        session.add_messages_async = AsyncMock()
        sessions = MagicMock()
        sessions.get = AsyncMock(return_value=session)
        sessions.commit_async = AsyncMock(return_value={"task_id": "t-1", "archive_uri": "a"})
        service = ResourceMemoryLinkService(session_service=sessions)
        service._read_resource_directory_abstract = AsyncMock(return_value="")
        never = asyncio.get_running_loop().create_future()
        service._wait_for_commit_task = AsyncMock(side_effect=lambda **_: never)
        with patch("openviking.service.resource_memory_link_service._resource_reason_peer_id",
                   return_value=None):
            result = await asyncio.wait_for(service.on_resource_added(
                ctx=MagicMock(), resource_uri="viking://resources/report.pdf", reason="r"), 2)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["commit_task_id"], "t-1")
        self.assertNotIn("commit_task", result)
        service._wait_for_commit_task.assert_not_called()
