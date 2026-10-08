"""2026-10-08: auto-captured agent artifacts never create reason commits."""
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
