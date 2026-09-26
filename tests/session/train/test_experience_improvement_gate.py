from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.models.jev import JevPayloadTooLarge
from openviking.session.memory.dataclass import MemoryFile, StoredLink
from openviking.session.train.components.experience_improvement_gate import (
    EXPERIENCE_GATE_CONTEXTS_KEY,
    ExperienceImprovementGate,
    _bounded_success_evidence,
)
from openviking.session.train.domain import PolicyPlanItem, PolicySet, PolicyUpdatePlan
from openviking.session.train.engine import PolicyTrainingEngine
from openviking.session.train.gradients import PatchSemanticGradient
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig

SESSION_URI = "viking://session/s1/archives/001"
EXPERIENCE_URI = "viking://user/u/memories/experiences/cancel.md"


def _source(instruction: str = "State the correct total") -> str:
    return f'''dag = workflow("Handle cancellation")
known = ask("Has the user supplied the reservation?")
report = tell("{instruction}")
known.then(report)'''


def _context(
    *,
    passed: bool,
    rollout_passed: bool | None = None,
    source_session_uri: str = SESSION_URI,
    experience_execution: dict | None = None,
) -> dict:
    return {
        "source_session_uri": source_session_uri,
        "session_summary": "The agent reported 708 instead of 1628.",
        "passed": passed,
        "rollout_passed": passed if rollout_passed is None else rollout_passed,
        "score": 1.0 if passed else 0.0,
        "feedback": [] if passed else ["Information 1628 was not communicated."],
        "experience_execution": experience_execution or {},
        "evidence": [
            {"id": "message:1", "kind": "user_message", "summary": "Cancel booking A."},
            {
                "id": "message:2",
                "kind": "assistant_message",
                "summary": "The total is 708.",
            },
        ],
    }


def _gradient(
    source: str,
    *,
    passed: bool,
    rollout_passed: bool | None = None,
    experience_uri: str = EXPERIENCE_URI,
    source_session_uri: str = SESSION_URI,
) -> PatchSemanticGradient:
    file = MemoryFile(
        uri=experience_uri,
        content=source,
        memory_type="experiences",
        extra_fields={"experience_name": "cancel"},
    )
    return PatchSemanticGradient(
        before_file=None,
        after_file=file,
        base_version=None,
        rationale="reflection",
        links=[
            StoredLink(
                from_uri=experience_uri,
                to_uri=source_session_uri,
                link_type="derived_from",
                weight=1.0,
            )
        ],
        confidence=0.8,
        metadata={
            EXPERIENCE_GATE_CONTEXTS_KEY: [
                _context(
                    passed=passed,
                    rollout_passed=rollout_passed,
                    source_session_uri=source_session_uri,
                )
            ]
        },
    )


def _plan(
    source: str,
    *,
    experience_uri: str = EXPERIENCE_URI,
    source_session_uri: str = SESSION_URI,
    target_name: str = "cancel",
    before_source: str | None = None,
) -> PolicyUpdatePlan:
    return PolicyUpdatePlan(
        items=[
            PolicyPlanItem(
                kind="upsert",
                memory_type="experiences",
                target_name=target_name,
                target_uri=experience_uri,
                before_content=before_source,
                after_content=source,
                links=[
                    StoredLink(
                        from_uri=experience_uri,
                        to_uri=source_session_uri,
                        link_type="derived_from",
                        weight=1.0,
                    )
                ],
            )
        ]
    )


class StopsAtReportDecider:
    async def decide(self, instances, *, evidence, context):
        del evidence, context
        instance = instances[0]
        if "known" not in instance.slot_values:
            return {instance.experience_uri: {"known": True}}
        return {}


class CompletesDecider:
    async def decide(self, instances, *, evidence, context):
        del evidence, context
        instance = instances[0]
        return {instance.experience_uri: {"known": True, "report": True}}


class BatchCompletesDecider:
    def __init__(self):
        self.calls = []

    async def decide(self, instances, *, evidence, context):
        self.calls.append((list(instances), evidence, context))
        return {instance.experience_uri: {"known": True, "report": True} for instance in instances}


class BatchStopsAtReportDecider:
    def __init__(self):
        self.calls = []

    async def decide(self, instances, *, evidence, context):
        self.calls.append((list(instances), evidence, context))
        return {
            instance.experience_uri: {"known": True}
            for instance in instances
            if "known" not in instance.slot_values
        }


@pytest.mark.asyncio
async def test_failed_session_accepts_relevant_corrective_action():
    jev = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "improvement_effective": {"type": "noul", "noul": 0.96},
                "preservation_0": {"type": "noul", "noul": 0.99},
            }
        )
    )
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=StopsAtReportDecider(),
        jev=jev,
    )
    source = _source("Report the total 1628 required by the failed trajectory")

    result = await gate.validate(
        _plan(source),
        [_gradient(source, passed=False)],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 1
    diagnostic = result.metadata["experience_improvement_gate"]
    assert diagnostic["passed"] is True
    replay = diagnostic["candidates"][0]["replays"][0]["replay"]
    assert replay["state"] == "running"
    assert replay["current_nodes"] == ["report"]
    jev.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_session_rejects_candidate_that_still_completes():
    jev = SimpleNamespace(evaluate=AsyncMock())
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=jev,
    )
    source = _source()

    result = await gate.validate(
        _plan(source),
        [_gradient(source, passed=False)],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert result.items == []
    diagnostic = result.metadata["experience_improvement_gate"]
    assert diagnostic["passed"] is False
    assert "accepts" in diagnostic["candidates"][0]["replays"][0]["reason"]
    jev.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_session_requires_candidate_to_complete():
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock(return_value={"preservation_0": {"noul": 0.99}})),
    )
    source = _source()

    result = await gate.validate(
        _plan(source),
        [_gradient(source, passed=True)],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 1
    assert result.metadata["experience_improvement_gate"]["passed"] is True


@pytest.mark.asyncio
async def test_merged_plan_keeps_independently_passing_candidate():
    second_experience = "viking://user/u/memories/experiences/refund.md"
    second_session = "viking://session/s2/archives/001"
    source = _source()
    plan = PolicyUpdatePlan(
        items=[
            *_plan(source).items,
            *_plan(
                source,
                experience_uri=second_experience,
                source_session_uri=second_session,
                target_name="refund",
            ).items,
        ]
    )
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchCompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock(return_value={"preservation_0": {"noul": 0.99}})),
    )

    result = await gate.validate(
        plan,
        [
            _gradient(source, passed=True),
            _gradient(
                source,
                passed=False,
                experience_uri=second_experience,
                source_session_uri=second_session,
            ),
        ],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert [item.target_uri for item in result.items] == [EXPERIENCE_URI]
    diagnostic = result.metadata["experience_improvement_gate"]
    assert diagnostic["passed"] is False
    assert diagnostic["atomic"] is False
    assert diagnostic["accepted_count"] == 1
    assert diagnostic["rejected_count"] == 1


@pytest.mark.asyncio
async def test_same_context_candidates_and_baselines_share_dag_replay_call():
    second_experience = "viking://user/u/memories/experiences/refund.md"
    second_session = "viking://session/s2/archives/001"
    baseline = _source("State the previous total")
    candidate = _source("State the corrected total")
    plan = PolicyUpdatePlan(
        items=[
            *_plan(candidate, before_source=baseline).items,
            *_plan(
                candidate,
                experience_uri=second_experience,
                source_session_uri=second_session,
                target_name="refund",
                before_source=baseline,
            ).items,
        ]
    )

    async def complete_all_slots(*, state, questions):
        del state
        return {
            question_id: {"type": question["type"], "noul": 0.99}
            for question_id, question in questions.items()
        }

    jev = SimpleNamespace(evaluate=AsyncMock(side_effect=complete_all_slots))
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        jev=jev,
    )

    result = await gate.validate(
        plan,
        [
            _gradient(candidate, passed=True),
            _gradient(
                candidate,
                passed=True,
                experience_uri=second_experience,
                source_session_uri=second_session,
            ),
        ],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 2
    assert jev.evaluate.await_count == 2
    assert len(jev.evaluate.await_args_list[0].kwargs["questions"]) == 8
    assert len(jev.evaluate.await_args_list[1].kwargs["questions"]) == 4
    diagnostics = result.metadata["experience_improvement_gate"]["candidates"]
    assert all(
        item["replays"][0]["baseline_replay"]["state"] == "completed" for item in diagnostics
    )


@pytest.mark.asyncio
async def test_failed_update_compares_candidate_with_baseline_and_execution_feedback():
    baseline = _source("Report whatever total is available")
    candidate = _source("Report the verified total 1628")
    execution = {
        EXPERIENCE_URI: {
            "revision": "before-update",
            "executed_nodes": ["known"],
            "current_nodes": ["report"],
        }
    }
    gradient = _gradient(candidate, passed=False)
    gradient.metadata[EXPERIENCE_GATE_CONTEXTS_KEY][0]["experience_execution"] = execution
    jev = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "improvement_effective": {"type": "noul", "noul": 0.97},
                "preservation_0": {"type": "noul", "noul": 0.99},
            }
        )
    )
    decider = BatchStopsAtReportDecider()
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=decider,
        jev=jev,
    )

    result = await gate.validate(
        _plan(candidate, before_source=baseline),
        [gradient],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 1
    assert len(decider.calls) == 2
    assert all(len(call[0]) == 2 for call in decider.calls)
    diagnostic = result.metadata["experience_improvement_gate"]["candidates"][0]["replays"][0]
    assert diagnostic["passed"] is True
    assert diagnostic["baseline_replay"]["current_nodes"] == ["report"]
    assert diagnostic["reason"] == "candidate improves the failed Session"
    judge_state = jev.evaluate.await_args.kwargs["state"]
    candidate_state = judge_state["candidates"]["improvement_effective"]
    assert json.loads(candidate_state["actual_experience_execution"]) == execution
    assert candidate_state["baseline_source"] == baseline
    assert candidate_state["candidate_source"] == candidate


@pytest.mark.asyncio
async def test_failed_candidates_share_corrective_action_jev_call():
    second_experience = "viking://user/u/memories/experiences/refund.md"
    second_session = "viking://session/s2/archives/001"
    candidate = _source("Report the verified total")
    plan = PolicyUpdatePlan(
        items=[
            *_plan(candidate).items,
            *_plan(
                candidate,
                experience_uri=second_experience,
                source_session_uri=second_session,
                target_name="refund",
            ).items,
        ]
    )
    jev = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "improvement_effective": {"type": "noul", "noul": 0.96},
                "preservation_0": {"type": "noul", "noul": 0.99},
                "improvement_effective_1": {"type": "noul", "noul": 0.95},
                "preservation_1": {"type": "noul", "noul": 0.99},
            }
        )
    )
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchStopsAtReportDecider(),
        jev=jev,
    )

    result = await gate.validate(
        plan,
        [
            _gradient(candidate, passed=False),
            _gradient(
                candidate,
                passed=False,
                experience_uri=second_experience,
                source_session_uri=second_session,
            ),
        ],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 2
    jev.evaluate.assert_awaited_once()
    call = jev.evaluate.await_args.kwargs
    assert set(call["questions"]) == {
        "improvement_effective",
        "improvement_effective_1",
        "preservation_0",
        "preservation_1",
    }
    assert set(call["state"]["candidates"]) == {"improvement_effective", "improvement_effective_1"}


@pytest.mark.asyncio
async def test_provider_oversize_splits_candidates_but_keeps_each_baseline_atomic():
    second_experience = "viking://user/u/memories/experiences/refund.md"
    second_session = "viking://session/s2/archives/001"
    baseline = _source("Report the previous total")
    candidate = _source("Report the verified total")
    plan = PolicyUpdatePlan(
        items=[
            *_plan(candidate, before_source=baseline).items,
            *_plan(
                candidate,
                experience_uri=second_experience,
                source_session_uri=second_session,
                target_name="refund",
                before_source=baseline,
            ).items,
        ]
    )

    async def reject_combined_batch(*, state, questions):
        if len(state["candidates"]) > 1:
            raise JevPayloadTooLarge("provider returned 422")
        question_id = next(iter(questions))
        candidate_state = state["candidates"][question_id]
        assert candidate_state["baseline_source"] == baseline
        return {qid: {"type": "noul", "noul": 0.96} for qid in questions}

    jev = SimpleNamespace(
        config=SimpleNamespace(max_input_tokens=100_000, model="qwen3-1.7b"),
        evaluate=AsyncMock(side_effect=reject_combined_batch),
    )
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchStopsAtReportDecider(),
        jev=jev,
    )

    result = await gate.validate(
        plan,
        [
            _gradient(candidate, passed=False),
            _gradient(
                candidate,
                passed=False,
                experience_uri=second_experience,
                source_session_uri=second_session,
            ),
        ],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 2
    diagnostics = result.metadata["experience_improvement_gate"]
    assert diagnostics["jev_batch_count"] == 3
    assert diagnostics["jev_split_count"] == 1
    assert diagnostics["oversized_candidates"] == []
    assert jev.evaluate.await_count == 3


@pytest.mark.asyncio
async def test_estimated_oversized_candidate_does_not_reject_other_candidate(monkeypatch):
    second_experience = "viking://user/u/memories/experiences/refund.md"
    second_session = "viking://session/s2/archives/001"
    candidate = _source("Report the verified total")
    plan = PolicyUpdatePlan(
        items=[
            *_plan(candidate).items,
            *_plan(
                candidate,
                experience_uri=second_experience,
                source_session_uri=second_session,
                target_name="refund",
            ).items,
        ]
    )

    def estimate_by_candidate(*, state, questions, model):
        del model
        if len(state["candidates"]) > 1 or "improvement_effective" in questions:
            return 200
        return 50

    monkeypatch.setattr(
        "openviking.session.train.components.experience_improvement_gate.estimate_jev_input_tokens",
        estimate_by_candidate,
    )
    jev = SimpleNamespace(
        config=SimpleNamespace(max_input_tokens=100, model="qwen3-1.7b"),
        evaluate=AsyncMock(
            return_value={
                "improvement_effective_1": {"type": "noul", "noul": 0.95},
                "preservation_1": {"type": "noul", "noul": 0.99},
            }
        ),
    )
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchStopsAtReportDecider(),
        jev=jev,
    )

    result = await gate.validate(
        plan,
        [
            _gradient(candidate, passed=False),
            _gradient(
                candidate,
                passed=False,
                experience_uri=second_experience,
                source_session_uri=second_session,
            ),
        ],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert [item.target_uri for item in result.items] == [second_experience]
    diagnostics = result.metadata["experience_improvement_gate"]
    assert diagnostics["jev_batch_count"] == 1
    assert diagnostics["jev_split_count"] == 1
    assert diagnostics["oversized_candidates"] == [
        {
            "experience_uri": EXPERIENCE_URI,
            "source_session_uri": SESSION_URI,
            "reason": "estimated input 200 exceeds configured Jev budget 100",
        }
    ]
    first_replay = diagnostics["candidates"][0]["replays"][0]
    assert "input is oversized" in first_replay["reason"]
    jev.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_compile_error_rejects_entire_experience_plan():
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock(return_value={"preservation_0": {"noul": 0.99}})),
    )
    source = "not valid Python ("

    result = await gate.validate(
        _plan(source),
        [_gradient(source, passed=False)],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert result.items == []
    replay = result.metadata["experience_improvement_gate"]["candidates"][0]["replays"][0]
    assert "does not compile" in replay["reason"]


@pytest.mark.asyncio
async def test_missing_jev_configuration_rejects_candidate():
    gate = ExperienceImprovementGate(config=DagDeciderConfig(provider="jev"))
    source = _source()

    result = await gate.validate(
        _plan(source),
        [_gradient(source, passed=False)],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert result.items == []
    diagnostic = result.metadata["experience_improvement_gate"]
    assert diagnostic["passed"] is False
    assert diagnostic["reason"] == "shared Jev configuration is missing"


@pytest.mark.asyncio
async def test_training_engine_runs_gate_before_updater():
    plan = _plan(_source())

    class LockablePolicySet:
        @asynccontextmanager
        async def lock(self):
            yield "lease"

        async def reload(self):
            return self

    optimizer = SimpleNamespace(plan=AsyncMock(return_value=plan))
    gated_plan = PolicyUpdatePlan(items=[], metadata={"gate": "rejected"})
    gate = SimpleNamespace(validate=AsyncMock(return_value=gated_plan))
    updater = SimpleNamespace(
        apply=AsyncMock(return_value=SimpleNamespace(updated_policy_set=LockablePolicySet()))
    )
    engine = PolicyTrainingEngine(
        rollout_analyzer=SimpleNamespace(),
        gradient_estimator=SimpleNamespace(),
        policy_optimizer=optimizer,
        policy_updater=updater,
        policy_update_gate=gate,
    )
    policy_set = LockablePolicySet()

    returned_plan, _ = await engine.plan_and_apply(
        gradients=[],
        policy_set=policy_set,
        ctx=SimpleNamespace(
            optimization_context="optimizer", gradient_context="gate", apply_context="apply"
        ),
    )

    assert returned_plan is gated_plan
    gate.validate.assert_awaited_once_with(plan, [], policy_set, "gate")
    updater.apply.assert_awaited_once_with(
        gated_plan,
        policy_set,
        "apply",
        transaction_handle="lease",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "success_complete,improve,preserve,accepted",
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
async def test_case_gate_aggregates_success_and_failed_sessions(
    success_complete, improve, preserve, accepted
):
    class EvidenceDecider:
        async def decide(self, instances, *, evidence, context):
            success = "successful" in context
            return {
                i.experience_uri: {"known": True, "report": success and success_complete}
                for i in instances
            }

    async def judge(*, state, questions):
        return {
            qid: {
                "noul": 0.99 if (preserve if qid.startswith("preservation") else improve) else 0.01
            }
            for qid in questions
        }

    source = _source()
    success = _gradient(
        source, passed=True, source_session_uri="viking://session/success/archives/1"
    )
    success.metadata[EXPERIENCE_GATE_CONTEXTS_KEY][0]["evidence"][1]["summary"] = "successful"
    failed = _gradient(source, passed=False)
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=EvidenceDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock(side_effect=judge)),
    )
    result = await gate.validate(
        _plan(source, before_source=source),
        [success, failed],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )
    assert bool(result.items) is accepted
    if accepted:
        saved = result.items[0].metadata["patch_metadata"]["source_sessions"]
        assert len(saved) == 2
        assert all("evidence" not in item for item in saved)


@pytest.mark.asyncio
async def test_unknown_session_requires_grounding_without_claiming_improvement():
    source = _source()
    gradient = _gradient(source, passed=None)

    async def judge(*, state, questions):
        assert state["candidates"]["improvement_effective"]["session_outcome"] is None
        return {qid: {"noul": 0.99} for qid in questions}

    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchStopsAtReportDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock(side_effect=judge)),
    )
    result = await gate.validate(_plan(source), [gradient], PolicySet("root", []), None)
    assert len(result.items) == 1
    assert result.metadata["experience_improvement_gate"]["candidates"][0]["improved"] is False


@pytest.mark.asyncio
async def test_failed_session_completion_never_counts_as_improvement():
    source = _source()

    async def judge(*, state, questions):
        return {qid: {"noul": 0.99} for qid in questions}

    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchCompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock(side_effect=judge)),
    )
    result = await gate.validate(
        _plan(source, before_source=source),
        [_gradient(source, passed=False)],
        PolicySet("root", []),
        None,
    )
    assert result.items == []
    diagnostic = result.metadata["experience_improvement_gate"]["candidates"][0]
    assert diagnostic["reason"] == "no failed source Session improved"


@pytest.mark.asyncio
async def test_empty_session_evidence_cannot_pass_gate():
    source = _source()
    gradient = _gradient(source, passed=True)
    gradient.metadata[EXPERIENCE_GATE_CONTEXTS_KEY][0]["evidence"] = []
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchCompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock()),
    )
    result = await gate.validate(_plan(source), [gradient], PolicySet("root", []), None)
    assert result.items == []
    assert (
        "no replay evidence"
        in result.metadata["experience_improvement_gate"]["candidates"][0]["replays"][0]["reason"]
    )


@pytest.mark.asyncio
async def test_failed_proposal_without_successful_anchor_is_rejected():
    source = _source("Report the verified total 1628")
    gradient = _gradient(source, passed=False)
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock()),
    )

    accepted, metadata = await gate.validate_proposals(
        [gradient],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert accepted == []
    diagnostic = metadata["experience_improvement_gate"]
    assert diagnostic["stage"] == "proposal"
    assert diagnostic["accepted_count"] == 0
    assert diagnostic["rejected_count"] == 1
    assert diagnostic["candidates"][0]["reason"] == (
        "failed proposal has no successful Session anchor"
    )
    gate.jev.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_proposal_uses_its_own_session_as_gate_evidence():
    source = _source("Report the verified total 1628")
    gradient = _gradient(source, passed=True)
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "improvement_effective": {"noul": 0.01},
                    "preservation_0": {"noul": 0.99},
                }
            )
        ),
    )

    accepted, metadata = await gate.validate_proposals(
        [gradient],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert accepted == [gradient]
    assert metadata["experience_improvement_gate"]["accepted_count"] == 1
    diagnostic = metadata["experience_improvement_gate"]["candidates"][0]
    assert diagnostic["gate_basis"] == "proposal_session"
    assert diagnostic["proposal_source_session_uri"] == SESSION_URI


@pytest.mark.asyncio
async def test_failed_proposal_gate_preserves_successful_session_anchor():
    from openviking.message import Message, TextPart
    from openviking.session.train.domain import Policy

    source = _source("Report the verified total 1628")
    gradient = _gradient(source, passed=False)
    policy = Policy(
        "cancel",
        EXPERIENCE_URI,
        2,
        "draft",
        _source("old"),
        metadata={
            "source_sessions": [
                {
                    "source_session_uri": "viking://user/u/sessions/old/history/archive_001",
                    "passed": True,
                }
            ]
        },
    )
    successful_message = Message(
        id="successful",
        role="assistant",
        parts=[TextPart(text="Reported the verified total 1628")],
    )
    fs = SimpleNamespace(read_file=AsyncMock(return_value=json.dumps(successful_message.to_dict())))
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "improvement_effective": {"noul": 0.99},
                    "preservation_0": {"noul": 0.99},
                }
            )
        ),
    )

    accepted, metadata = await gate.validate_proposals(
        [gradient],
        PolicySet("viking://user/u/memories/experiences", [policy], viking_fs=fs),
        None,
    )

    assert accepted == [gradient]
    source_session = gradient.metadata["validated_source_sessions"][0]
    assert source_session["source_session_uri"] == SESSION_URI
    assert source_session["passed"] is False
    anchors = gradient.metadata["proposal_gate_anchor_sessions"]
    assert anchors[0]["source_session_uri"] == ("viking://user/u/sessions/old/history/archive_001")
    diagnostic = metadata["experience_improvement_gate"]["candidates"][0]
    assert diagnostic["gate_basis"] == "successful_session_regression"
    assert diagnostic["proposal_source_session_uri"] == SESSION_URI
    fs.read_file.assert_awaited_once()


def test_successful_session_witness_is_bounded_and_drops_system_messages():
    evidence = [
        {"id": "system", "kind": "system_message", "summary": "policy" * 10_000},
        {"id": "first", "kind": "user_message", "summary": "initial request"},
        *[
            {
                "id": f"tool-{index}",
                "kind": "tool_result",
                "summary": "tool result " * 1_000,
            }
            for index in range(20)
        ],
        {"id": "last", "kind": "assistant_message", "summary": "successful response"},
    ]

    bounded = _bounded_success_evidence(evidence)

    assert all(item["kind"] != "system_message" for item in bounded)
    assert sum(len(item["summary"]) for item in bounded) <= 20_000
    assert bounded[0]["id"] == "first"
    assert bounded[-1]["id"] == "last"
