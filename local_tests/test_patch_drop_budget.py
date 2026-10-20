"""Failed-patch operations are dropped, not the archive; long-term batches follow the input budget."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from openviking.session.memory.dataclass import ResolvedOperation, ResolvedOperations
from openviking.session.memory.extract_loop import ExtractLoop
import openviking_cli.utils.config as config_module
from openviking.session.memory.context_budget import long_term_fallback_batch_tokens


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
        return patch.object(config_module, "get_openviking_config", return_value=config)

    def test_batches_follow_input_budget(self):
        with self.budget(160_000):
            self.assertEqual(long_term_fallback_batch_tokens(), 80_000)

    def test_floors_keep_previous_sizes(self):
        with self.budget(16_000):
            self.assertEqual(long_term_fallback_batch_tokens(), 48_000)
