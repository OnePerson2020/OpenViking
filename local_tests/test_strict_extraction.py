import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from jsonschema import ValidationError
from openviking.models.vlm.base import ToolCall, VLMResponse
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.extraction_output_protocol.strict_json_protocol import (
    StrictJsonExtractionOutputProtocol, strict_schema, strict_loads,
)
from openviking.session.memory.tools import MEMORY_TOOLS_REGISTRY
from openviking.session.memory.context_budget import MemoryInputBudgetError
from protocol_fixtures import _context, _preference_schema


class ProtocolTests(TestCase):
    def setUp(self):
        self.ctx = _context([_preference_schema()])
        self.protocol = StrictJsonExtractionOutputProtocol()
        self.tools = [MEMORY_TOOLS_REGISTRY['read'].to_schema()]
        self.fmt = self.protocol.response_format(self.ctx, self.tools)
        self.payload = {'action': {'operations': {'preferences': [], 'delete_ids': []}}}

    def parse(self, payload=None, response=None, tools=None):
        return self.protocol.parse_response(
            response or VLMResponse(content=json.dumps(self.payload if payload is None else payload)),
            self.ctx, self.fmt, self.tools if tools is None else tools)

    def test_empty_operations_are_explicit_not_fabricated(self):
        calls, ops = self.parse()
        self.assertIsNone(calls)
        self.assertEqual(ops.model_dump(), {'preferences': [], 'delete_ids': []})

    def test_create_with_embedded_python_fences_is_json_data(self):
        self.payload['action']['operations']['preferences'] = [
            {'page_id': 100, 'topic': 'code', 'content': 'Example:\n```python\nx=1\n```', 'score': 0}]
        _, ops = self.parse()
        self.assertIn('```python', ops.preferences[0].content)

    def test_search_replace_remains_typed_patch(self):
        self.ctx.page_id_map.get_page_id('viking://user/a/memories/preferences/editor.md')
        self.payload['action']['operations']['preferences'] = [
            {'page_id': 1, 'topic': 'editor', 'content': {'blocks': [{'search': 'Vim', 'replace': 'Emacs'}]}, 'score': None}]
        _, ops = self.parse()
        self.assertEqual(ops.preferences[0].content.blocks[0].search, 'Vim')

    def test_missing_fields_rejected_before_compatibility_defaults(self):
        with self.assertRaises(ValidationError): self.parse({'action': {'operations': {}}})

    def test_extra_fields_rejected(self):
        self.payload['action']['operations']['unknown'] = []
        with self.assertRaises(ValidationError): self.parse()

    def test_string_page_id_not_coerced(self):
        self.payload['action']['operations']['preferences'] = [{'page_id': '100', 'topic': 'x', 'content': 'x', 'score': None}]
        with self.assertRaises(ValidationError): self.parse()

    def test_invalid_json_no_repair(self):
        with self.assertRaises(ValueError): self.parse(response=VLMResponse(content='{"action":'))

    def test_fenced_json_refused(self):
        with self.assertRaises(ValueError): self.parse(response=VLMResponse(content='```json\n{}\n```'))

    def test_truncated_but_valid_json_refused(self):
        with self.assertRaises(ValueError): self.parse(response=VLMResponse(content=json.dumps(self.payload), finish_reason='length'))

    def test_plain_string_without_metadata_refused(self):
        with self.assertRaises(ValueError): self.protocol.parse_response(json.dumps(self.payload), self.ctx, self.fmt, self.tools)

    def test_native_tool_wrapper_refused(self):
        with self.assertRaises(ValueError): self.parse(response=VLMResponse(content='{}', tool_calls=[ToolCall('x', 'read', {})]))

    def tool_payload(self, name='read'):
        return {'action': {'tool_calls': [{'name': name, 'arguments': {'uri': 'viking://user/a/memories/x.md', 'offset': None, 'limit': None, 'field': None, 'text_offset': None}}]}}

    def test_read_defaults_preserved(self):
        calls, ops = self.parse(self.tool_payload())
        self.assertIsNone(ops)
        self.assertEqual(calls[0].arguments, {'uri': 'viking://user/a/memories/x.md'})

    def test_unallowed_tool_refused(self):
        with self.assertRaises(ValidationError): self.parse(self.tool_payload('write'))

    def test_required_tool_uri_cannot_be_null(self):
        data = self.tool_payload(); data['action']['tool_calls'][0]['arguments']['uri'] = None
        with self.assertRaises(ValidationError): self.parse(data)

    def test_tool_arguments_checked_before_execution(self):
        data = self.tool_payload(); data['action']['tool_calls'][0]['arguments']['offset'] = -1
        with self.assertRaises(ValidationError): self.parse(data)

    def test_tools_excluded_when_final(self):
        fmt = self.protocol.response_format(self.ctx, [])
        with self.assertRaises(ValidationError): self.protocol.parse_response(VLMResponse(content=json.dumps(self.tool_payload())), self.ctx, fmt, [])

    def test_cannot_mix_tools_and_operations(self):
        self.payload['action']['tool_calls'] = self.tool_payload()['action']['tool_calls']
        with self.assertRaises(ValidationError): self.parse()

    def test_tool_list_bounded(self):
        data = self.tool_payload(); data['action']['tool_calls'] *= 9
        with self.assertRaises(ValidationError): self.parse(data)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(ValueError): strict_loads('{"a":1,"a":2}')

    def test_nan_rejected(self):
        with self.assertRaises(ValueError): strict_loads('{"a":NaN}')

    def test_open_dictionary_schema_refused(self):
        with self.assertRaises(ValueError): strict_schema({'type': 'object', 'additionalProperties': {'type': 'string'}})

    def test_new_memory_schema_nullable_value_is_preserved(self):
        self.payload['action']['operations']['preferences'] = [
            {'page_id': 100, 'topic': 'editor', 'content': None, 'score': 0}]
        _, model = self.parse()
        self.assertIsNone(model.preferences[0].content)

    def test_new_memory_cannot_be_patch(self):
        self.payload['action']['operations']['preferences'] = [
            {'page_id': 100, 'topic': 'editor', 'content': {'blocks':[{'search':'x','replace':'y'}]}, 'score': 0}]
        with self.assertRaises(ValueError): self.parse()

    def test_duplicate_pages_rejected(self):
        item = {'page_id': 100, 'topic': 'editor', 'content': 'text', 'score': 0}
        self.payload['action']['operations']['preferences'] = [item, item]
        with self.assertRaises(ValueError): self.parse()

    def test_unread_delete_rejected(self):
        self.payload['action']['operations']['delete_ids'] = [{'delete_page_id': 1, 'replacement_page_id': None}]
        with self.assertRaises(ValueError): self.parse()

    def test_new_low_page_id_rejected(self):
        self.payload['action']['operations']['preferences'] = [{'page_id': 3, 'topic': 'x', 'content': 'x', 'score': 0}]
        with self.assertRaises(ValueError): self.parse()

    def test_provider_schema_has_no_conditionals_but_they_still_apply(self):
        self.assertNotIn('"if"', json.dumps(self.fmt))
        self.payload['action']['operations']['preferences'] = [
            {'page_id': 100, 'topic': None, 'content': 'x', 'score': 0}]
        with self.assertRaises(ValueError): self.parse()

    def test_original_schema_unmodified(self):
        old = self.ctx.operations_model.model_json_schema(); snapshot = deepcopy(old)
        strict_schema(old); self.assertEqual(old, snapshot)


class LoopTests(IsolatedAsyncioTestCase):
    def make_loop(self, response=None):
        ctx = _context([_preference_schema()])
        model = SimpleNamespace(model='offline', supports_structured_output=True,
                                get_completion_async=AsyncMock(return_value=response))
        loop = ExtractLoop(model, viking_fs=object())
        loop._output_context = ctx
        loop._output_protocol = StrictJsonExtractionOutputProtocol()
        loop._tool_schemas = [MEMORY_TOOLS_REGISTRY['read'].to_schema()]
        return loop, model

    async def test_request_uses_response_format_not_native_tools(self):
        loop, model = self.make_loop(VLMResponse(content='{"action":{"operations":{"preferences":[],"delete_ids":[]}}}'))
        _, ops = await loop._call_llm([{'role': 'user', 'content': 'test'}])
        kwargs = model.get_completion_async.call_args.kwargs
        self.assertTrue(kwargs['response_format']['json_schema']['strict'])
        self.assertNotIn('tools', kwargs)
        self.assertIsNotNone(ops)

    async def test_invalid_schema_recorded_without_output_body(self):
        loop, model = self.make_loop(VLMResponse(content='secret private malformed output'))
        self.assertEqual(await loop._call_llm([]), (None, None))
        self.assertNotIn('secret', loop._last_parse_error)
        self.assertEqual(loop._last_llm_failure_content, '')

    async def test_deadline_not_reclassified_as_format_error(self):
        loop, model = self.make_loop();model.get_completion_async.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError): await loop._call_llm([])
        self.assertEqual(model.get_completion_async.call_count, 1)

    async def test_cancellation_propagates(self):
        loop, model = self.make_loop();model.get_completion_async.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await loop._call_llm([])

    async def test_budget_includes_schema(self):
        loop, model = self.make_loop()
        cfg = SimpleNamespace(memory=SimpleNamespace(extraction_input_token_budget=1))
        with patch('openviking.session.memory.extract_loop.get_openviking_config', return_value=cfg):
            with self.assertRaises(MemoryInputBudgetError): await loop._call_llm([])
        model.get_completion_async.assert_not_called()

    async def test_unsupported_backend_fails_closed(self):
        loop, model = self.make_loop();model.supports_structured_output = False
        with self.assertRaises(ValueError): await loop._call_llm([])
        model.get_completion_async.assert_not_called()

    async def test_disabled_tools_not_exposed_in_schema(self):
        loop, model = self.make_loop(VLMResponse(content='{"action":{"operations":{"preferences":[],"delete_ids":[]}}}'))
        loop._disable_tools_for_iteration = True
        await loop._call_llm([])
        branches = model.get_completion_async.call_args.kwargs['response_format']['json_schema']['schema']['properties']['action']['anyOf']
        self.assertEqual(len(branches), 1)
