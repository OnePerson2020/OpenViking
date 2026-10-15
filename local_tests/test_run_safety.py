from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.dataclass import ResolvedOperations
from protocol_fixtures import _preference_schema, PageIdMap


class RunSafetyTests(IsolatedAsyncioTestCase):
    def make(self, content):
        ctx=SimpleNamespace(page_id_map=PageIdMap())
        provider=SimpleNamespace(get_memory_schemas=lambda ctx:[_preference_schema()],
            get_output_language=lambda:'en',get_tools=lambda:[],get_extract_context=lambda:ctx,
            read_file_contents={},partial_read_fields={},instruction=lambda:'Grounded memory extraction',
            prefetch=AsyncMock(return_value=[]))
        vlm=SimpleNamespace(model='fake',get_completion_async=AsyncMock(return_value=content))
        loop=ExtractLoop(vlm,viking_fs=Mock(),context_provider=provider)
        ops=ResolvedOperations(upsert_operations=[],delete_file_contents=[],errors=[])
        loop.resolve_operations=AsyncMock(return_value=(ops,[]))
        loop._check_unread_existing_files=AsyncMock(return_value={})
        loop._validate_patch_operations=AsyncMock(return_value=[])
        loop._partial_operation_errors=Mock(return_value=[])
        loop.finalize_operations=AsyncMock()
        cfg=SimpleNamespace(memory=SimpleNamespace(link_enabled=False,extraction_output_format='json',extraction_input_token_budget=128000),vlm=SimpleNamespace(max_tokens=None))
        return loop,vlm,cfg

    async def invoke(self,loop,cfg):
        with patch('openviking.session.memory.extract_loop.get_openviking_config',return_value=cfg),patch('openviking_cli.utils.config.get_openviking_config',return_value=cfg):
            return await loop.run()

    async def test_repeated_invalid_json_never_finalizes(self):
        loop,vlm,cfg=self.make('not json')
        with self.assertRaisesRegex(ValueError,'output invalid after 4 iterations'):await self.invoke(loop,cfg)
        self.assertEqual(vlm.get_completion_async.await_count,4)
        loop.resolve_operations.assert_not_awaited();loop.finalize_operations.assert_not_awaited()

    async def test_valid_explicit_empty_operations_still_valid(self):
        loop,vlm,cfg=self.make('{"preferences":[],"delete_ids":[]}')
        await self.invoke(loop,cfg)
        self.assertEqual(vlm.get_completion_async.await_count,1)
        loop.finalize_operations.assert_awaited_once()

    async def test_partial_read_guard_still_blocks(self):
        loop,vlm,cfg=self.make('{"preferences":[],"delete_ids":[]}')
        loop._partial_operation_errors.return_value=[{'reason':'partial field cannot be replaced'}]
        with self.assertRaises(ValueError):await self.invoke(loop,cfg)
        loop.finalize_operations.assert_not_awaited()
