"""2026-10-06: event page-id renumbering, unsafe-deletion drop, terminal salvage,
patch-failure drop and budget-derived batch sizes."""
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from openviking.models.vlm.base import VLMResponse
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation, ResolvedOperations
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.extraction_output_protocol.strict_json_protocol import (
    StrictActionError, StrictJsonExtractionOutputProtocol,
)
from openviking.session.memory.memory_type_registry import get_default_registry
from protocol_fixtures import _context, _existing_preference, _preference_schema
import openviking.session.session as session_module


def _event(page_id, name="deploy finished"):
    return {"page_id": page_id, "event_name": name, "goal": "g", "summary": "s",
            "ranges": "0-1"}


def _pref(page_id, topic="editor", content="vim"):
    return {"page_id": page_id, "topic": topic, "content": content, "score": 0}


class RenumberTests(TestCase):
    def setUp(self):
        self.protocol = StrictJsonExtractionOutputProtocol()
        self.events = get_default_registry().get("events")

    def parse(self, ctx, operations):
        # delete_ids exists only when a deletable (non add-only) type is present.
        extra = {"delete_ids": []} if "preferences" in operations else {}
        raw = {"action": {"operations": {**extra, **operations}}}
        return self.protocol.parse_response(
            VLMResponse(content=json.dumps(raw)), ctx, self.protocol.response_format(ctx, []), [])

    def test_new_event_ids_below_100_and_repeats_are_renumbered(self):
        ctx = _context([self.events])
        _, ops = self.parse(ctx, {"events": [_event(1, "a"), _event(1, "b"), _event(0, "c")]})
        ids = [e.page_id for e in ops.events]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(i >= 100 for i in ids))

    def test_event_id_taken_by_new_mutable_item_is_renumbered(self):
        ctx = _context([_preference_schema(), self.events])
        _, ops = self.parse(ctx, {"preferences": [_pref(100)], "events": [_event(100)]})
        self.assertEqual(ops.preferences[0].page_id, 100)
        self.assertNotEqual(ops.events[0].page_id, 100)

    def test_event_on_existing_page_becomes_a_new_page(self):
        # 2026-10-10: the model kept reusing an existing add-only page id until
        # ADD_ONLY_EXISTING_PAGE failed the archive (trajectories, 11x that day).
        uri = "viking://user/a/memories/events/2026/10/06/x.md"
        existing = MemoryFile(uri=uri, memory_type="events", content="old", extra_fields={})
        ctx = _context([self.events], files=[existing])
        existing_id = ctx.page_id_map.get_page_id(uri)
        _, ops = self.parse(ctx, {"events": [_event(existing_id, "a"), _event(150, "b")]})
        ids = [e.page_id for e in ops.events]
        self.assertNotIn(existing_id, ids)
        self.assertIn(150, ids)
        self.assertTrue(all(i >= 100 for i in ids))
        self.assertIsNone(ctx.page_id_map.resolve(ids[0]))

    def test_mutable_small_unknown_id_still_needs_retry(self):
        ctx = _context([_preference_schema()])
        with self.assertRaisesRegex(StrictActionError, "NEW_PAGE_ID_RANGE"):
            self.parse(ctx, {"preferences": [_pref(5)]})


class SalvageTests(TestCase):
    def setUp(self):
        self.protocol = StrictJsonExtractionOutputProtocol()
        self.ctx = _context([_preference_schema()])

    def fail(self, operations):
        raw = {"action": {"operations": {"delete_ids": [], **operations}}}
        with self.assertRaises(Exception):
            self.protocol.parse_response(VLMResponse(content=json.dumps(raw)), self.ctx,
                                         self.protocol.response_format(self.ctx, []), [])

    def test_keeps_valid_items_and_drops_business_invalid_one(self):
        bad = _pref(101, "theme", {"blocks": [{"search": "a", "replace": "b"}]})
        self.fail({"preferences": [_pref(100), bad]})
        model, dropped = self.protocol.salvage(self.ctx)
        self.assertEqual([p.page_id for p in model.preferences], [100])
        self.assertEqual(dropped, ["preferences[1]:NEW_VALUE_IS_PATCH"])

    def test_drops_schema_invalid_item(self):
        self.fail({"preferences": [{"topic": "x", "content": "y", "score": 0}, _pref(100)]})
        model, dropped = self.protocol.salvage(self.ctx)
        self.assertEqual([p.page_id for p in model.preferences], [100])
        self.assertEqual(dropped, ["preferences[0]:required"])

    def test_nothing_left_keeps_failure_visible(self):
        self.fail({"preferences": [_pref(5)]})
        model, dropped = self.protocol.salvage(self.ctx)
        self.assertIsNone(model)
        self.assertEqual(dropped, ["preferences[0]:NEW_PAGE_ID_RANGE"])

    def test_no_operations_after_invalid_json(self):
        with self.assertRaises(Exception):
            self.protocol.parse_response(VLMResponse(content="not json"), self.ctx,
                                         self.protocol.response_format(self.ctx, []), [])
        self.assertEqual(self.protocol.salvage(self.ctx), (None, []))


class DeletionDropTests(TestCase):
    def test_undeletable_type_is_dropped_not_fatal(self):
        uri = "viking://user/a/memories/preferences/editor.md"
        ctx = _context([_preference_schema()], files=[_existing_preference(uri, "editor", "b")])
        ctx.memory_type_resolver = lambda _uri: None
        protocol = StrictJsonExtractionOutputProtocol()
        raw = {"action": {"operations": {"preferences": [_pref(100)],
                                         "delete_ids": [{"delete_page_id": 1, "replacement_page_id": None}]}}}
        with self.assertLogs("openviking.session.memory.extraction_output_protocol.strict_json_protocol",
                             level="WARNING"):
            _, ops = protocol.parse_response(VLMResponse(content=json.dumps(raw)), ctx,
                                             protocol.response_format(ctx, []), [])
        self.assertEqual(len(ops.delete_ids), 0)
        self.assertEqual(len(ops.preferences), 1)


class PatchDropTests(IsolatedAsyncioTestCase):
    def ops(self):
        a = ResolvedOperation(memory_fields={}, memory_type="preferences", uris=["u1"], page_id=1)
        b = ResolvedOperation(memory_fields={}, memory_type="preferences", uris=["u2"], page_id=2)
        return ResolvedOperations(upsert_operations=[a, b], delete_file_contents=[], errors=[])

    async def test_failed_patch_operation_dropped_rest_kept(self):
        ops = self.ops()
        self.assertTrue(ExtractLoop._drop_failed_patch_operations(ops, [{"uri": "u1", "page_id": 1}]))
        self.assertEqual([o.page_id for o in ops.upsert_operations], [2])

    async def test_all_failed_is_reported(self):
        ops = self.ops()
        errors = [{"uri": "u1", "page_id": 1}, {"uri": "u2", "page_id": 2}]
        self.assertFalse(ExtractLoop._drop_failed_patch_operations(ops, errors))
        self.assertEqual(len(ops.upsert_operations), 2)


class BudgetTests(TestCase):
    def budget(self, value):
        config = SimpleNamespace(memory=SimpleNamespace(extraction_input_token_budget=value))
        return patch.object(session_module, "get_openviking_config", return_value=config)

    def test_batches_follow_input_budget(self):
        with self.budget(160_000):
            self.assertEqual(session_module._long_term_fallback_batch_tokens(), 80_000)

    def test_floors_keep_previous_sizes(self):
        with self.budget(16_000):
            self.assertEqual(session_module._long_term_fallback_batch_tokens(), 48_000)
