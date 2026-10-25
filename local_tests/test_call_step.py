"""model-calls rows name the step that made them (2026-10-25 amplification split)."""
import asyncio
from types import SimpleNamespace

import pytest

from openviking.models import call_diagnostics as cd
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.telemetry import bind_telemetry_stage


def _obs():
    return cd.CallObservation(model="m", input_tokens=1, max_tokens=9, tool_names=[], thinking=False, timeout=1)


def test_step_label_falls_back_to_telemetry_stage():
    assert _obs().fields["step"] is None
    with bind_telemetry_stage("semantic_execute"):
        assert _obs().fields["step"] == "semantic_execute"
        with cd.call_step("working_memory"):
            assert _obs().fields["step"] == "working_memory"
        assert _obs().fields["step"] == "semantic_execute"


class AgentTrajectoryContextProvider:  # name is what gets logged
    pass


def test_extract_loop_labels_calls_with_provider():
    seen = []

    class _VLM:
        async def get_completion_async(self, **_):
            seen.append(_obs().fields["step"])
            raise RuntimeError("stop")

    loop = object.__new__(ExtractLoop)
    loop.vlm, loop.context_provider = _VLM(), AgentTrajectoryContextProvider()
    loop._disable_tools_for_iteration, loop._tool_schemas = False, None
    loop.thinking, loop._effective_max_output_tokens = False, 9
    with pytest.raises(RuntimeError):
        asyncio.run(loop._call_llm([{"role": "user", "content": "hi"}]))
    assert seen == ["AgentTrajectoryContextProvider"]
