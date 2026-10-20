"""Offline regression tests. No live storage or provider calls."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.dataclass import ResolvedOperations
from openviking.session.memory.extraction_output_protocol.python_protocol import PythonExtractionOutputProtocol
from openviking.retrieve.context_assembler.expansion import expand_queries

# Vendored pure upstream fixtures; no server fixtures or live storage.
import protocol_fixtures
fixtures = vars(protocol_fixtures)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        uri = 'viking://user/test/memories/preferences/editor.md'
        self.context = fixtures['_context'](
            [fixtures['_preference_schema']()],
            files=[fixtures['_existing_preference'](uri, 'editor', 'Use Vim')],
        )
        self.protocol = PythonExtractionOutputProtocol()
        self.binding = self.protocol.render_new_bindings(self.context, source='test')

    def test_existing_update_still_compiles(self):
        operations, error = self.protocol.parse(
            'preferences_1.content.edit(search="Vim", replace="Neovim")', self.context)
        self.assertIsNone(error)
        self.assertIsNotNone(operations)

    def test_forged_binding_still_rejected(self):
        operations, error = self.protocol.parse(
            'x = sdk.existing(memory_type="preferences", content="evil")', self.context)
        self.assertIsNone(operations)
        self.assertIn('reserved', error)

class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    def make_loop(self):
        context = SimpleNamespace(page_id_map=fixtures['PageIdMap']())
        provider = SimpleNamespace(
            get_memory_schemas=lambda ctx: [fixtures['_preference_schema']()],
            get_output_language=lambda: 'en', get_tools=lambda: [],
            get_extract_context=lambda: context, read_file_contents={},
            instruction=lambda: 'Extract grounded preferences.', prefetch=AsyncMock(return_value=[]),
        )
        loop = ExtractLoop(vlm=SimpleNamespace(model='mock'), viking_fs=Mock(), context_provider=provider, max_iterations=3)
        return loop

    async def test_terminal_parse_error_raises_before_finalize(self):
        loop = self.make_loop()
        loop._last_llm_failure_kind = 'parse_error'
        loop._last_parse_error = 'sdk.existing() is reserved'
        loop._call_llm = AsyncMock(return_value=(None, None))
        loop.finalize_operations = AsyncMock()
        config = SimpleNamespace(memory=SimpleNamespace(link_enabled=False, extraction_output_format='python'), vlm=SimpleNamespace(max_tokens=None))
        with patch('openviking.session.memory.extract_loop.get_openviking_config', return_value=config), patch('openviking_cli.utils.config.get_openviking_config', return_value=config):
            with self.assertRaisesRegex(ValueError, 'output invalid after 4 iterations'):
                await loop.run()
        self.assertEqual(loop._call_llm.await_count, 4)
        loop.finalize_operations.assert_not_awaited()
        messages = loop._call_llm.await_args.args[0]
        self.assertEqual(sum('previous output was not a valid' in m.get('content', '') for m in messages), 4)

    async def test_valid_empty_program_is_still_success(self):
        loop = self.make_loop()
        operations = ResolvedOperations(upsert_operations=[], delete_file_contents=[], errors=[])
        loop._call_llm = AsyncMock(return_value=(None, object()))
        loop.resolve_operations = AsyncMock(return_value=(operations, []))
        loop._retryable_resolution_issues = Mock(return_value=[])
        loop._check_unread_existing_files = AsyncMock(return_value={})
        loop._validate_patch_operations = AsyncMock(return_value=[])
        loop._partial_operation_errors = Mock(return_value=[])
        loop.finalize_operations = AsyncMock()
        config = SimpleNamespace(memory=SimpleNamespace(link_enabled=False, extraction_output_format='python'), vlm=SimpleNamespace(max_tokens=None))
        with patch('openviking.session.memory.extract_loop.get_openviking_config', return_value=config), patch('openviking_cli.utils.config.get_openviking_config', return_value=config):
            result, _ = await loop.run()
        self.assertIs(result, operations)
        loop.finalize_operations.assert_awaited_once()


class ExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_named_and_falls_back(self):
        async def slow(**kwargs):
            await asyncio.sleep(10)
        analyzer = SimpleNamespace(analyze=slow)
        session = SimpleNamespace(get_context_for_search=AsyncMock(return_value={'current_messages': ['context']}))
        with patch('openviking.retrieve.context_assembler.expansion.IntentAnalyzer', return_value=analyzer), self.assertLogs('openviking.retrieve.context_assembler.expansion', level='WARNING') as logs:
            queries, status = await expand_queries(query='original', session=session, timeout_s=.01, planner=object())
        self.assertEqual((queries, status), (['original'], 'failed'))
        self.assertIn('error_type=TimeoutError', logs.output[0])


if __name__ == '__main__':
    unittest.main()
