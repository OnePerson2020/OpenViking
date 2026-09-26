from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_fakes import fake_request_context

from openviking.message import Message, TextPart
from openviking.session.train import Case, Rollout, Rubric, RubricEvaluation
from openviking.session.train.components.session_analyzer import (
    SessionAnalyzerContext,
    SessionRolloutAnalyzer,
)


@pytest.mark.asyncio
async def test_session_analysis_preserves_raw_evidence_without_writing_memories():
    fs = SimpleNamespace(write_file=AsyncMock(side_effect=AssertionError("no intermediate writes")))
    case = Case("case_a", "cancel", {}, Rubric("r", "", []))
    rollout = Rollout(
        case,
        [Message(id="m", role="user", parts=[TextPart(text="cancel")])],
        "p0",
        metadata={
            "source_session_uri": "viking://session/s/archives/1",
            "experience_execution": {"exp": {"executed_nodes": ["identify"]}},
        },
    )
    analyzer = SessionRolloutAnalyzer(viking_fs=fs)
    result = await analyzer.analyze(rollout, SessionAnalyzerContext(fake_request_context()))
    assert result.rollout is rollout
    assert result.evaluation is None
    assert result.trajectories == []
    assert result.metadata["experience_execution"]["exp"]["executed_nodes"] == ["identify"]
    fs.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_session_analysis_uses_explicit_evaluation():
    evaluation = RubricEvaluation(False, 0.0, [], ["not cancelled"])
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p", evaluation)
    result = await SessionRolloutAnalyzer().analyze(
        rollout, SessionAnalyzerContext(fake_request_context())
    )
    assert result.evaluation is evaluation


@pytest.mark.asyncio
async def test_optional_skill_extraction_exposes_only_skill_schema(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.session.memory.dataclass import ResolvedOperation, ResolvedOperations
    from openviking.session.train.components import session_analyzer as module
    from openviking_cli.session.user_id import UserIdentifier

    observed = []

    class Loop:
        def __init__(self, **kwargs):
            observed.append(kwargs)

        async def run(self):
            return ResolvedOperations(
                upsert_operations=[
                    ResolvedOperation(
                        memory_type="session_skills",
                        uris=["viking://user/u/skills/check/SKILL.md"],
                        memory_fields={"skill_name": "check", "content": "Check results"},
                    )
                ],
                delete_file_contents=[],
                errors=[],
            ), []

    monkeypatch.setattr(module, "ExtractLoop", Loop)
    ctx = RequestContext(user=UserIdentifier("default", "u"), role=Role.ROOT)
    fs = SimpleNamespace(write_file=AsyncMock())
    result = await SessionRolloutAnalyzer(
        viking_fs=fs, vlm=SimpleNamespace()
    ).extract_session_skills(messages=[], ctx=ctx)
    assert [s.memory_type for s in observed[0]["context_provider"].get_memory_schemas(ctx)] == [
        "session_skills"
    ]
    assert result["skill_gradients"][0].after_file.memory_type == "skills"
    fs.write_file.assert_not_called()
