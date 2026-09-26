from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_fakes import fake_request_context

from openviking.message import Message, TextPart
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation, ResolvedOperations
from openviking.session.train import (
    Case,
    ExperienceGradientContext,
    ExperienceGradientEstimator,
    ExperienceSet,
    Rollout,
    RolloutAnalysis,
    Rubric,
    RubricEvaluation,
)
from openviking.session.train.components.experience_improvement_gate import (
    EXPERIENCE_GATE_CONTEXTS_KEY,
)

ROOT = "viking://user/u/memories/experiences"
SESSION = "viking://session/s1/archives/001"


def analysis(name="case_a", session=SESSION, passed=True):
    case = Case(
        name=name,
        task_signature="cancel duplicate",
        input={},
        rubric=Rubric(name="done", description="", criteria=[]),
    )
    rollout = Rollout(
        case=case,
        messages=[
            Message(id="m1", role="user", parts=[TextPart(text="Cancel the duplicate booking.")])
        ],
        policy_snapshot_id="p0",
    )
    return RolloutAnalysis(
        rollout=rollout,
        evaluation=(
            None
            if passed is None
            else RubricEvaluation(
                passed=passed,
                score=float(passed),
                criterion_results=[],
                feedback=[] if passed else ["duplicate remains"],
            )
        ),
        metadata={
            "source_session_uri": session,
            "case_uri": f"viking://user/u/memories/cases/{name}.md",
        },
    )


def operations(name="case_a", **fields):
    return ResolvedOperations(
        upsert_operations=[
            ResolvedOperation(
                memory_type="experiences",
                uris=[f"{ROOT}/{name}.md"],
                memory_fields={
                    "experience_name": name,
                    "content": 'dag = workflow("cancel")\ncancel = call("cancel", "duplicate")',
                    **fields,
                },
            )
        ],
        delete_file_contents=[],
        errors=[],
    )


async def estimate(estimator, item):
    return await estimator.estimate(
        item,
        ExperienceSet(root_uri=ROOT, policies=[]),
        ExperienceGradientContext(request_context=fake_request_context(user_id="u"), messages=[]),
    )


@pytest.mark.asyncio
async def test_direct_session_proposal_has_fixed_case_and_archive_provenance(monkeypatch):
    run = AsyncMock(return_value=operations())
    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", run)
    item = analysis(passed=False)
    gradients = await estimate(ExperienceGradientEstimator(), item)
    assert item.trajectories == []
    assert len(gradients) == 1
    proposal = gradients[0]
    assert proposal.target_name == item.rollout.case.name
    assert proposal.links[0].to_uri == SESSION
    gate_context = proposal.metadata[EXPERIENCE_GATE_CONTEXTS_KEY][0]
    assert gate_context["passed"] is False
    assert gate_context["feedback"] == ["duplicate remains"]
    assert gate_context["evidence"][0]["summary"] == "Cancel the duplicate booking."
    provider = run.await_args.args[0]
    assert provider.target_uri == proposal.target_uri
    assert provider.messages == item.rollout.messages


@pytest.mark.asyncio
async def test_unknown_outcome_is_not_promoted_to_success(monkeypatch):
    monkeypatch.setattr(
        ExperienceGradientEstimator, "_run_extract_loop", AsyncMock(return_value=operations())
    )
    (proposal,) = await estimate(ExperienceGradientEstimator(), analysis(passed=None))
    assert proposal.metadata[EXPERIENCE_GATE_CONTEXTS_KEY][0]["passed"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["rename", "extra", "delete", "cross_case", "supersedes", "errors"]
)
async def test_invalid_target_changes_fail_explicitly(monkeypatch, invalid):
    ops = operations()
    if invalid == "rename":
        ops.upsert_operations[0].memory_fields["experience_name"] = "other"
    if invalid == "extra":
        ops.upsert_operations.append(operations("other").upsert_operations[0])
    if invalid == "delete":
        ops.delete_file_contents.append(MemoryFile(uri=f"{ROOT}/case_a.md", content="old"))
    if invalid == "cross_case":
        ops.upsert_operations[0].uris = [f"{ROOT}/other.md"]
    if invalid == "supersedes":
        ops.upsert_operations[0].memory_fields["supersedes"] = "other"
    if invalid == "errors":
        ops.errors.append("invalid SDK")
    monkeypatch.setattr(
        ExperienceGradientEstimator, "_run_extract_loop", AsyncMock(return_value=ops)
    )
    with pytest.raises(ValueError):
        await estimate(ExperienceGradientEstimator(), analysis())


@pytest.mark.asyncio
async def test_concurrent_sessions_propose_same_case_without_waiting_for_trials(monkeypatch):
    entered = 0
    ready = asyncio.Event()

    async def run(self, provider, context):
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        return operations()

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", run)
    batches = await asyncio.gather(
        estimate(ExperienceGradientEstimator(), analysis()),
        estimate(ExperienceGradientEstimator(), analysis(session=SESSION.replace("s1", "s2"))),
    )
    assert batches[0][0].target_uri == batches[1][0].target_uri
    assert batches[0][0].links[0].to_uri != batches[1][0].links[0].to_uri


@pytest.mark.asyncio
async def test_estimator_wires_session_provider_into_extract_loop(monkeypatch):
    from types import SimpleNamespace

    from openviking.server.identity import RequestContext, Role
    from openviking.session.train.components import gradient_estimator as module
    from openviking_cli.session.user_id import UserIdentifier

    recorded = []

    class Loop:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

        async def run(self):
            return operations(), []

    monkeypatch.setattr(module, "ExtractLoop", Loop)
    fs = SimpleNamespace()
    ctx = RequestContext(user=UserIdentifier("default", "u"), role=Role.ROOT)
    resolver = SimpleNamespace(get_vlm=AsyncMock(return_value=SimpleNamespace(model="test")))
    estimator = ExperienceGradientEstimator(viking_fs=fs, vlm_resolver=resolver)
    result = await estimator.estimate(
        analysis(), ExperienceSet(ROOT, []), ExperienceGradientContext(ctx, [])
    )
    provider = recorded[0]["context_provider"]
    assert [schema.memory_type for schema in provider.get_memory_schemas(ctx)] == ["experiences"]
    assert provider.case.name == result[0].target_name
    assert provider.source_session_uri == SESSION
    assert provider._viking_fs is fs
    resolver.get_vlm.assert_awaited_once_with("default")
