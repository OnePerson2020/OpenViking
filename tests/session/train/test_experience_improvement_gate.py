from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.session.memory.dataclass import MemoryFile, StoredLink
from openviking.session.train.components.experience_improvement_gate import (
    EXPERIENCE_GATE_CONTEXTS_KEY,
    ExperienceImprovementGate,
)
from openviking.session.train.domain import PolicyPlanItem, PolicySet, PolicyUpdatePlan
from openviking.session.train.engine import PolicyTrainingEngine
from openviking.session.train.gradients import PatchSemanticGradient
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig

TRAJECTORY_URI = "viking://user/u/memories/trajectories/cancel.md"
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
    trajectory_uri: str = TRAJECTORY_URI,
    experience_execution: dict | None = None,
) -> dict:
    return {
        "trajectory_uri": trajectory_uri,
        "trajectory_summary": "The agent reported 708 instead of 1628.",
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
    trajectory_uri: str = TRAJECTORY_URI,
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
                to_uri=trajectory_uri,
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
                    trajectory_uri=trajectory_uri,
                )
            ]
        },
    )


def _plan(
    source: str,
    *,
    experience_uri: str = EXPERIENCE_URI,
    trajectory_uri: str = TRAJECTORY_URI,
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
                        to_uri=trajectory_uri,
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
async def test_failed_trajectory_accepts_relevant_corrective_action():
    jev = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "improvement_effective": {"type": "noul", "noul": 0.96},
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
async def test_failed_trajectory_rejects_candidate_that_still_completes():
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
    assert "still accepts" in diagnostic["candidates"][0]["replays"][0]["reason"]
    jev.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_trajectory_requires_candidate_to_complete():
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock()),
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
async def test_failed_rollout_can_reattribute_mislabeled_successful_trajectory():
    jev = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "improvement_effective": {"type": "noul", "noul": 0.93},
            }
        )
    )
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=StopsAtReportDecider(),
        jev=jev,
    )
    source = _source("Report the total 1628 required by the failed rollout")

    result = await gate.validate(
        _plan(source),
        [_gradient(source, passed=True, rollout_passed=False)],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    replay = result.metadata["experience_improvement_gate"]["candidates"][0]["replays"][0]
    assert len(result.items) == 1
    assert replay["passed"] is True
    assert replay["failure_reattributed"] is True
    assert replay["reason"] == "candidate correction is attributable to the failed rollout"


@pytest.mark.asyncio
async def test_merged_plan_keeps_independently_passing_candidate():
    second_experience = "viking://user/u/memories/experiences/refund.md"
    second_trajectory = "viking://user/u/memories/trajectories/refund.md"
    source = _source()
    plan = PolicyUpdatePlan(
        items=[
            *_plan(source).items,
            *_plan(
                source,
                experience_uri=second_experience,
                trajectory_uri=second_trajectory,
                target_name="refund",
            ).items,
        ]
    )
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=BatchCompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock(return_value={})),
    )

    result = await gate.validate(
        plan,
        [
            _gradient(source, passed=True),
            _gradient(
                source,
                passed=False,
                experience_uri=second_experience,
                trajectory_uri=second_trajectory,
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
    second_trajectory = "viking://user/u/memories/trajectories/refund.md"
    baseline = _source("State the previous total")
    candidate = _source("State the corrected total")
    plan = PolicyUpdatePlan(
        items=[
            *_plan(candidate, before_source=baseline).items,
            *_plan(
                candidate,
                experience_uri=second_experience,
                trajectory_uri=second_trajectory,
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
                trajectory_uri=second_trajectory,
            ),
        ],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 2
    jev.evaluate.assert_awaited_once()
    assert len(jev.evaluate.await_args.kwargs["questions"]) == 8
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
        evaluate=AsyncMock(return_value={"improvement_effective": {"type": "noul", "noul": 0.97}})
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
    assert diagnostic["reason"] == "candidate improves on the baseline for the failed rollout"
    judge_state = jev.evaluate.await_args.kwargs["state"]
    candidate_state = judge_state["candidates"]["improvement_effective"]
    assert json.loads(candidate_state["actual_experience_execution"]) == execution
    assert candidate_state["baseline_source"] == baseline
    assert candidate_state["candidate_source"] == candidate


@pytest.mark.asyncio
async def test_failed_candidates_share_corrective_action_jev_call():
    second_experience = "viking://user/u/memories/experiences/refund.md"
    second_trajectory = "viking://user/u/memories/trajectories/refund.md"
    candidate = _source("Report the verified total")
    plan = PolicyUpdatePlan(
        items=[
            *_plan(candidate).items,
            *_plan(
                candidate,
                experience_uri=second_experience,
                trajectory_uri=second_trajectory,
                target_name="refund",
            ).items,
        ]
    )
    jev = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "improvement_effective": {"type": "noul", "noul": 0.96},
                "improvement_effective_1": {"type": "noul", "noul": 0.95},
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
                trajectory_uri=second_trajectory,
            ),
        ],
        PolicySet(root_uri="viking://user/u/memories/experiences", policies=[]),
        None,
    )

    assert len(result.items) == 2
    jev.evaluate.assert_awaited_once()
    call = jev.evaluate.await_args.kwargs
    assert set(call["questions"]) == {"improvement_effective", "improvement_effective_1"}
    assert set(call["state"]["candidates"]) == set(call["questions"])


@pytest.mark.asyncio
async def test_compile_error_rejects_entire_experience_plan():
    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=CompletesDecider(),
        jev=SimpleNamespace(evaluate=AsyncMock()),
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
