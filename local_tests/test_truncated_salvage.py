"""2026-10-10: terminal salvage of complete items from a truncated (finish_reason=length)
response. Shapes follow the runaway samples in logs/truncated-outputs.jsonl."""
import json
from unittest import TestCase

from openviking.models.vlm.base import VLMResponse
from openviking.session.memory.extraction_output_protocol.strict_json_protocol import (
    StrictJsonExtractionOutputProtocol, truncated_operations,
)
from protocol_fixtures import _context, _preference_schema


def _pref(page_id, topic="editor", content="vim"):
    return {"page_id": page_id, "topic": topic, "content": content, "score": 0}


def _head(**lists):
    body = ",".join(f'"{k}":[' + ",".join(json.dumps(i) for i in v) for k, v in lists.items())
    return '{"action":{"operations":{' + body


class TruncatedOperationsTests(TestCase):
    def test_whitespace_loop_after_complete_items(self):
        text = _head(preferences=[_pref(100), _pref(101, "theme")]) + "\n  " * 5000
        self.assertEqual(truncated_operations(text), {"preferences": [_pref(100), _pref(101, "theme")]})

    def test_repeated_key_loop_keeps_closed_lists(self):
        text = ('{"action":{"operations":{"soul":[],"preferences":[' + json.dumps(_pref(100))
                + '],"delete_ids"  ,"delete_ids"  ,"delete_ids"  ,"delete')
        self.assertEqual(truncated_operations(text), {"preferences": [_pref(100)]})

    def test_item_cut_mid_value_is_not_kept(self):
        text = _head(preferences=[_pref(100)]) + ',{"page_id":101,"topic":"th'
        self.assertEqual(truncated_operations(text), {"preferences": [_pref(100)]})

    def test_tool_call_action_and_garbage_give_nothing(self):
        self.assertIsNone(truncated_operations('{"action":{"tool_calls":[{"name":"read"' + "\n" * 100))
        self.assertIsNone(truncated_operations("\n" * 100))
        self.assertIsNone(truncated_operations('{"action":{"operations":{"preferences":[{"page'))
        self.assertIsNone(truncated_operations(None))


class TruncatedParseTests(TestCase):
    def setUp(self):
        self.protocol = StrictJsonExtractionOutputProtocol()
        self.ctx = _context([_preference_schema()])
        self.fmt = self.protocol.response_format(self.ctx, [])

    def parse(self, content, finish_reason):
        return self.protocol.parse_response(
            VLMResponse(content=content, finish_reason=finish_reason), self.ctx, self.fmt, [])

    def test_truncated_response_still_fails_but_salvage_keeps_complete_items(self):
        text = '{"action":{"operations":{"delete_ids":[],"preferences":[' + json.dumps(_pref(100)) + "\n" * 300
        with self.assertRaisesRegex(Exception, "RESPONSE_NOT_COMPLETE"):
            self.parse(text, "length")
        model, dropped = self.protocol.salvage(self.ctx)
        self.assertEqual([p.page_id for p in model.preferences], [100])
        self.assertEqual(dropped, [])

    def test_earlier_response_operations_are_not_reused(self):
        bad = {"action": {"operations": {"delete_ids": [], "preferences": [_pref(5)]}}}
        with self.assertRaises(Exception):
            self.parse(json.dumps(bad), "stop")
        with self.assertRaisesRegex(Exception, "RESPONSE_NOT_COMPLETE"):
            self.parse('{"action":{"tool_calls":[' + " " * 50, "length")
        self.assertEqual(self.protocol.salvage(self.ctx), (None, []))
        with self.assertRaises(Exception):
            self.parse(json.dumps(bad), "stop")
        with self.assertRaisesRegex(Exception, "RESPONSE_METADATA_MISSING"):
            self.protocol.parse_response("plain text", self.ctx, self.fmt, [])
        self.assertEqual(self.protocol.salvage(self.ctx), (None, []))
