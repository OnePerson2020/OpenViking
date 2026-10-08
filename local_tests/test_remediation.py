"""Offline regression tests. No live storage or provider calls."""
import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openviking.message import Message
from openviking.message.part import TextPart
from openviking.session.session import Session, WM_SEVEN_SECTIONS, WM_UPDATE_TOOL
from openviking.models.vlm.base import VLMResponse, ToolCall
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.dataclass import ResolvedOperations
from openviking.session.memory.extraction_output_protocol.python_protocol import PythonExtractionOutputProtocol
from openviking.retrieve.context_assembler.expansion import expand_queries

# Vendored pure upstream fixtures; no server fixtures or live storage.
import protocol_fixtures
fixtures = vars(protocol_fixtures)


def wm(label='facts'):
    return '\n\n'.join(f'## {name}\n{label}' for name in WM_SEVEN_SECTIONS)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        uri = 'viking://user/test/memories/preferences/editor.md'
        self.context = fixtures['_context'](
            [fixtures['_preference_schema']()],
            files=[fixtures['_existing_preference'](uri, 'editor', 'Use Vim')],
        )
        self.protocol = PythonExtractionOutputProtocol()
        self.binding = self.protocol.render_new_bindings(self.context, source='test')

    def test_snapshot_is_not_executable_constructor(self):
        self.assertNotIn('= sdk.existing(', self.binding)
        snapshot = json.loads(self.binding.splitlines()[-1])
        self.assertEqual(snapshot['bound_variable'], 'preferences_1')
        self.assertEqual(snapshot['visible_fields']['content'], 'Use Vim')
        self.assertNotIn('version', snapshot['visible_fields'])

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

    def test_final_instruction_has_only_available_binding_names(self):
        text = self.protocol.render_final_instruction(self.context)
        self.assertIn('preferences_1', text)
        self.assertNotIn('preferences_2', text)

    def test_unknown_name_retry_is_actionable(self):
        text = self.protocol.render_format_retry("Line 1: unknown name 'invented'")
        self.assertIn('Do not derive variable names', text)


class WorkingMemoryTests(unittest.IsolatedAsyncioTestCase):
    def config(self, network):
        return SimpleNamespace(
            output_language_override='en',
            memory=SimpleNamespace(extraction_input_token_budget=128000),
            vlm=SimpleNamespace(is_available=lambda: True, get_completion_async=network),
        )

    def test_nested_headings_demoted_losslessly(self):
        original = wm().replace('## Key Facts & Decisions\nfacts', '## Key Facts & Decisions\nfacts\n## extra detail\nimportant content')
        result = Session._normalize_working_memory_headings(original)
        self.assertEqual(result.replace('### extra detail', '## extra detail'), original)
        self.assertIn('important content', result)
        Session._validate_complete_working_memory(result)

    def test_missing_and_duplicate_sections_are_not_repaired(self):
        for original in [wm().replace('## Open Issues', '### Open Issues'), wm() + '\n## Open Issues\nduplicate']:
            self.assertEqual(Session._normalize_working_memory_headings(original), original)
            with self.assertRaises(ValueError):
                Session._validate_complete_working_memory(original)

    async def test_missing_tool_payload_recovers_strict_json(self):
        missing = VLMResponse(finish_reason='tool_calls')
        payload = {'sections': {name: {'op': 'KEEP'} for name in WM_SEVEN_SECTIONS}}
        network = AsyncMock(side_effect=[missing, json.dumps(payload)])
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            result = await Session._complete_wm_tool_request(object.__new__(Session), 'source', WM_UPDATE_TOOL, {'type': 'function'})
        self.assertTrue(result.has_tool_calls)
        self.assertEqual(result.tool_calls[0].arguments, payload)
        self.assertEqual(network.await_count, 2)
        self.assertNotIn('tools', network.await_args.kwargs)

    async def test_partial_json_not_promoted_to_tool_success(self):
        missing = VLMResponse(finish_reason='tool_calls')
        network = AsyncMock(side_effect=[missing, '{"sections": {"Current State": {"op": "KEEP"}}}'])
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            result = await Session._complete_wm_tool_request(object.__new__(Session), 'source', WM_UPDATE_TOOL, {'type': 'function'})
        self.assertIs(result, missing)
        self.assertFalse(result.has_tool_calls)

    async def test_valid_native_tool_not_retried(self):
        response = VLMResponse(tool_calls=[ToolCall('id', 'update_working_memory', {
            'sections': {name: {'op': 'KEEP'} for name in WM_SEVEN_SECTIONS}
        })])
        network = AsyncMock(return_value=response)
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            result = await Session._complete_wm_tool_request(object.__new__(Session), 'source', WM_UPDATE_TOOL, {'type': 'function'})
        self.assertIs(result, response)
        network.assert_awaited_once()

    async def test_missing_section_retries_and_validates(self):
        network = AsyncMock(side_effect=[wm().replace('## Open Issues', '### Open Issues'), wm('fixed')])
        session = object.__new__(Session)
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            result = await session._complete_working_memory_creation('original grounded conversation')
        self.assertEqual(result, wm('fixed'))
        self.assertEqual(network.await_count, 2)
        correction = network.await_args_list[1].args[0]
        self.assertIn('## Open Issues', correction)
        self.assertIn('original grounded conversation', correction)

    async def test_repeated_invalid_output_fails_without_fabrication(self):
        network = AsyncMock(return_value='## Session Title\npartial')
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            with self.assertRaisesRegex(ValueError, 'seven required'):
                await Session._complete_working_memory_creation(object.__new__(Session), 'source')
        self.assertEqual(network.await_count, 2)

    async def test_transport_failure_not_format_retried(self):
        network = AsyncMock(side_effect=ConnectionError('offline'))
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            with self.assertRaises(ConnectionError):
                await Session._complete_working_memory_creation(object.__new__(Session), 'source')
        network.assert_awaited_once()

    async def test_valid_output_byte_preserved(self):
        network = AsyncMock(return_value=wm())
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            result = await Session._complete_working_memory_creation(object.__new__(Session), 'source')
        self.assertEqual(result, wm())
        network.assert_awaited_once()

    async def test_budget_blocks_network(self):
        network = AsyncMock()
        config = self.config(network)
        config.memory.extraction_input_token_budget = 10
        with patch('openviking.session.session.get_openviking_config', return_value=config):
            with self.assertRaisesRegex(ValueError, 'budget exceeded'):
                await Session._complete_working_memory_creation(object.__new__(Session), 'data ' * 1000)
        network.assert_not_awaited()

    async def test_cancellation_not_swallowed(self):
        network = AsyncMock(side_effect=asyncio.CancelledError())
        with patch('openviking.session.session.get_openviking_config', return_value=self.config(network)):
            with self.assertRaises(asyncio.CancelledError):
                await Session._complete_working_memory_creation(object.__new__(Session), 'source')


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
