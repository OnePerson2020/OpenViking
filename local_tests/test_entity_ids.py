"""Look-alike entity pairing: names with different ids never pair; a shared date is not an id."""
import unittest

from openviking.session.memory.extract_loop import _entity_ids_compatible as ok


class EntityIds(unittest.TestCase):
    def test_rule(self):
        self.assertFalse(ok("job_1521824.md", "job_1521825.md"))
        self.assertFalse(ok("atomic_20260914_extract_016_s05.md", "atomic_20260914_extract_017_s05.md"))
        self.assertFalse(ok("handoff_20261013.md", "handoff_20261014.md"))
        self.assertFalse(ok("exp_flight_10000001.md", "job_template_20000001.md"))
        self.assertTrue(ok("forge_job_1513378.md", "job_1513378_e2.md"))
        self.assertTrue(ok("商品等权_balance_product.md", "商品等权loss设计.md"))
        self.assertTrue(ok("dorado_task_102896937.md", "task_102896937.md"))
