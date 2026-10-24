"""Repair prompts must render issues holding pydantic patch objects (StrPatch)."""
import pytest

from openviking.session.memory.extraction_output_protocol import create_extraction_output_protocol
from openviking.session.memory.merge_op.base import StrPatch


@pytest.mark.parametrize("fmt", ["json", "python"])
def test_repair_prompts_serialize_strpatch(fmt):
    patch = StrPatch(blocks=[{"search": "old line", "replace": "new line"}])
    issue = {"memory_type": "events", "page_id": 1, "reason": "x", "operation": {"content": patch}}
    protocol = create_extraction_output_protocol(fmt)
    assert "old line" in protocol.render_resolution_repair([issue])
    assert "old line" in protocol.render_patch_repair([issue])
