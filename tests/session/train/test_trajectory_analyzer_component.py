# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.session.memory.dataclass import ResolvedOperation, ResolvedOperations
from openviking.session.train import (
    Case,
    CriterionResult,
    Rollout,
    Rubric,
    RubricEvaluation,
)
from openviking.session.train.components.trajectory_analyzer import (
    TrajectoryAnalyzerContext,
    TrajectoryRolloutAnalyzer,
    _experience_execution_from_rollout,
)


def _patch_runtime_defaults(monkeypatch):
    config = SimpleNamespace(
        output_language_override="",
        language_fallback="en",
        memory=SimpleNamespace(
            eager_prefetch=False,
            prefetch_search_topn=5,
            link_enabled=False,
            custom_templates_dir=None,
            experimental_memory_switch=False,
        ),
    )
    monkeypatch.setattr(
        "openviking.session.memory.utils.language.get_openviking_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "openviking.session.memory.session_extract_context_provider.get_openviking_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "openviking_cli.utils.config.get_openviking_config",
        lambda: config,
    )


class FakeExtractLoop:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._transaction_handle = None
        FakeExtractLoop.created.append(self)

    async def run(self):
        return (
            ResolvedOperations(
                upsert_operations=[
                    ResolvedOperation(
                        old_memory_file_content=None,
                        memory_fields={
                            "trajectory_name": "task",
                            "outcome": "success",
                            "retrieval_anchor": "Stage: final",
                            "content": "# task\nbody",
                        },
                        memory_type="trajectories",
                        uris=["viking://user/u/memories/trajectories/task_20260607120000.md"],
                        page_id=100,
                    )
                ],
                delete_file_contents=[],
                errors=[],
                resolved_links=[],
            ),
            [],
        )


class FakeSkillOnlyExtractLoop(FakeExtractLoop):
    async def run(self):
        return (
            ResolvedOperations(
                upsert_operations=[
                    ResolvedOperation(
                        old_memory_file_content=None,
                        memory_fields={
                            "skill_name": "code-review",
                            "description": "Review code carefully",
                            "content": "## Workflow\n- Read the changed files first.",
                        },
                        memory_type="session_skills",
                        uris=["viking://user/u/skills/code-review/SKILL.md"],
                        page_id=100,
                    )
                ],
                delete_file_contents=[],
                errors=[],
                resolved_links=[],
            ),
            [],
        )


class FakeVikingFS:
    agfs = None

    def __init__(self):
        self.files = {}
        self.writes = []

    async def read_file(self, uri, ctx=None):
        return self.files[uri]

    async def write_file(self, uri, content, ctx=None, lock_handle=None, lease_ref=None):
        del lock_handle, lease_ref
        self.files[uri] = content
        self.writes.append((uri, content, ctx))


class FakeRolloutEvaluator:
    def __init__(self):
        self.calls = []

    async def evaluate(self, rollout, context):
        self.calls.append((rollout, context))
        return RubricEvaluation(
            passed=False,
            score=0.25,
            criterion_results=[
                CriterionResult(
                    criterion_name="tau2_reward",
                    passed=False,
                    score=0.0,
                    feedback=["reward was zero"],
                    evidence=["missing confirmation"],
                )
            ],
            feedback=["task failed"],
            metadata={"source": "fake"},
        )


def _rollout() -> Rollout:
    return Rollout(
        case=Case(
            name="case",
            task_signature="task",
            input={},
            rubric=Rubric(name="r", description="d", criteria=[]),
        ),
        messages=[
            Message(
                id="m",
                role="user",
                parts=[TextPart(text="hello")],
                created_at="2026-06-07T12:00:00",
            )
        ],
        policy_snapshot_id="snapshot",
    )


@pytest.mark.asyncio
async def test_trajectory_rollout_analyzer_extracts_and_persists_trajectory(monkeypatch):
    from openviking.session.train.components import trajectory_analyzer as module

    _patch_runtime_defaults(monkeypatch)
    FakeExtractLoop.created.clear()
    fs = FakeVikingFS()
    account_vlm = SimpleNamespace(
        model="account-vlm",
        get_vlm_instance=lambda: SimpleNamespace(model="account-vlm"),
    )

    class FakeResolver:
        async def get_vlm(self, account_id):
            assert account_id == "default"
            return account_vlm

    monkeypatch.setattr(module, "ExtractLoop", FakeExtractLoop)
    monkeypatch.setattr(module, "get_viking_fs", lambda: fs)

    analyzer = TrajectoryRolloutAnalyzer(viking_fs=fs, vlm_resolver=FakeResolver())
    context = TrajectoryAnalyzerContext(
        request_context=SimpleNamespace(
            user=SimpleNamespace(account_id="default", user_id="u"),
            account_id="default",
        ),
        source_archive_uri="viking://user/u/sessions/s1/history/archive_001",
    )

    rollout = _rollout()
    experience_uri = "viking://user/u/memories/experiences/exchange.md"
    rollout.metadata["dag_runtime"] = {
        "session_id": "tau2_dag_1",
        "events": [
            {
                "experience_uri": experience_uri,
                "state": "running",
                "revision": 1,
                "slot_values": {"reservation_known": True},
                "slot_evidence": {"reservation_known": ["message:1"]},
                "executed_nodes": [1],
                "current_nodes": [2],
                "waiting_for_context": [3],
                "node_slots": {
                    1: "reservation_known",
                    2: "cancel",
                    3: "confirmation",
                },
                "actions": [{"node_id": 2, "slot_name": "cancel"}],
                "completed_nodes": [{"node_id": 1, "slot_name": "reservation_known"}],
                "action_outcomes": [{"node_id": 2, "slot_name": "cancel", "status": "issued"}],
                "evidence": [{"id": "message:1", "summary": "private raw context"}],
            }
        ],
    }
    rollout.messages.append(
        Message(
            id="tool-result",
            role="user",
            parts=[
                ToolPart(
                    tool_id="read-1",
                    tool_name="read",
                    tool_input={"uri": experience_uri},
                    tool_output="experience body",
                    tool_status="completed",
                )
            ],
        )
    )

    analysis = await analyzer.analyze(rollout, context)

    assert FakeExtractLoop.created
    created_loop = FakeExtractLoop.created[0]
    assert created_loop._transaction_handle is None
    provider = created_loop.kwargs["context_provider"]
    assert provider._transaction_handle is None
    assert provider._vlm_config is account_vlm
    assert [
        schema.memory_type for schema in provider.get_memory_schemas(context.request_context)
    ] == ["trajectories"]
    assert len(fs.writes) == 1
    assert fs.writes[0][0] == "viking://user/u/memories/trajectories/task_20260607120000.md"
    assert '"case_name": "case"' in fs.writes[0][1]
    assert (
        '"source_archive_uri": "viking://user/u/sessions/s1/history/archive_001"' in fs.writes[0][1]
    )
    assert '"experience_execution": "{' in fs.writes[0][1]
    assert '"source_experience_uris"' not in fs.writes[0][1]
    assert '"source_session_id"' not in fs.writes[0][1]
    assert '"source_messages_uri"' not in fs.writes[0][1]
    assert '"source_task_id"' not in fs.writes[0][1]
    assert '"source_trace_id"' not in fs.writes[0][1]
    assert len(analysis.trajectories) == 1
    traj = analysis.trajectories[0]
    assert traj.name == "task"
    assert traj.outcome == "success"
    assert traj.retrieval_anchor == "Stage: final"
    assert traj.metadata["case_name"] == "case"
    execution = json.loads(traj.metadata["experience_execution"])
    assert execution[experience_uri] == {
        "action_outcomes": [{"node_id": 2, "slot_name": "cancel", "status": "issued"}],
        "actions": [{"node_id": 2, "slot_name": "cancel"}],
        "completed_nodes": [{"node_id": 1, "slot_name": "reservation_known"}],
        "current_nodes": ["cancel"],
        "executed_nodes": ["reservation_known"],
        "experience_name": "exchange",
        "revision": 1,
        "slot_evidence": {"reservation_known": ["message:1"]},
        "slot_values": {"reservation_known": True},
        "state": "running",
        "waiting_for_context": ["confirmation"],
    }
    assert "private raw context" not in traj.metadata["experience_execution"]
    assert analysis.metadata["experience_execution"] == execution
    assert analysis.evaluation.passed is True
    assert analysis.metadata["policy_snapshot_id"] == "snapshot"


def test_experience_execution_uses_latest_snapshot_per_experience():
    rollout = _rollout()
    uri = "viking://user/u/memories/experiences/cancel.md"
    rollout.metadata["dag_runtime"] = {
        "events": [
            {
                "experience_uri": uri,
                "state": "running",
                "revision": 1,
                "executed_nodes": [1],
                "current_nodes": [2],
                "slot_values": {"known": True},
                "node_slots": {1: "known", 2: "cancel"},
            },
            {
                "experience_uri": uri,
                "state": "completed",
                "revision": 2,
                "executed_nodes": [1, 2],
                "current_nodes": [],
                "slot_values": {"known": True, "cancelled": True},
                "node_slots": {1: "known", 2: "cancelled"},
            },
            {"state": "search_failed", "error": "no Experience URI"},
        ]
    }

    assert _experience_execution_from_rollout(rollout) == {
        uri: {
            "experience_name": "cancel",
            "state": "completed",
            "revision": 2,
            "executed_nodes": ["known", "cancelled"],
            "current_nodes": [],
            "slot_values": {"known": True, "cancelled": True},
        }
    }


def test_experience_execution_accepts_precomputed_commit_snapshot():
    rollout = _rollout()
    expected = {
        "viking://user/u/memories/experiences/cancel.md": {
            "state": "running",
            "executed_nodes": ["known"],
            "current_nodes": ["cancel"],
        }
    }
    rollout.metadata = {
        "experience_execution": expected,
        "dag_runtime": {"events": [{"experience_uri": "ignored"}]},
    }

    assert _experience_execution_from_rollout(rollout) == expected


@pytest.mark.asyncio
async def test_trajectory_rollout_analyzer_extracts_skill_without_persisting_trajectory(
    monkeypatch,
):
    from openviking.session.train.components import trajectory_analyzer as module

    _patch_runtime_defaults(monkeypatch)
    FakeExtractLoop.created.clear()
    fs = FakeVikingFS()
    monkeypatch.setattr(module, "ExtractLoop", FakeSkillOnlyExtractLoop)
    monkeypatch.setattr(module, "get_viking_fs", lambda: fs)

    analyzer = TrajectoryRolloutAnalyzer(viking_fs=fs, vlm=SimpleNamespace(model="fake"))
    result = await analyzer.extract_trajectory_memories(
        messages=_rollout().messages,
        ctx=SimpleNamespace(
            user=SimpleNamespace(account_id="default", user_id="u"),
            account_id="default",
        ),
        include_trajectories=False,
        include_session_skills=True,
    )

    provider = FakeExtractLoop.created[0].kwargs["context_provider"]
    assert [
        schema.memory_type
        for schema in provider.get_memory_schemas(
            SimpleNamespace(user=SimpleNamespace(account_id="default", user_id="u"))
        )
    ] == ["session_skills"]
    assert result["contexts"] == []
    assert len(result["skill_gradients"]) == 1
    assert result["skill_gradients"][0].after_file.memory_type == "skills"
    assert fs.writes == []


@pytest.mark.asyncio
async def test_trajectory_rollout_analyzer_evaluates_before_extracting_trajectory(monkeypatch):
    from openviking.session.train.components import trajectory_analyzer as module

    _patch_runtime_defaults(monkeypatch)
    FakeExtractLoop.created.clear()
    fs = FakeVikingFS()
    evaluator = FakeRolloutEvaluator()
    evaluator_context = {"benchmark": "tau2"}
    monkeypatch.setattr(module, "ExtractLoop", FakeExtractLoop)
    monkeypatch.setattr(module, "get_viking_fs", lambda: fs)

    analyzer = TrajectoryRolloutAnalyzer(
        viking_fs=fs,
        vlm=SimpleNamespace(model="fake"),
        evaluator=evaluator,
    )
    context = TrajectoryAnalyzerContext(
        request_context=SimpleNamespace(
            user=SimpleNamespace(account_id="default", user_id="u"),
            account_id="default",
        ),
        evaluator_context=evaluator_context,
    )

    rollout = _rollout()
    analysis = await analyzer.analyze(rollout, context)

    assert evaluator.calls == [(rollout, evaluator_context)]
    assert analysis.evaluation.score == 0.25
    assert analysis.evaluation.metadata == {"source": "fake"}
    created_loop = FakeExtractLoop.created[0]
    provider = created_loop.kwargs["context_provider"]
    assert len(provider.messages) == 2
    assert provider.messages[0] is rollout.messages[0]
    feedback_message = provider.messages[1]
    assert feedback_message.role == "user"
    assert "[Rollout Evaluation]" in feedback_message.content
    assert "score: 0.25" in feedback_message.content
    assert "task failed" in feedback_message.content
    assert "missing confirmation" in feedback_message.content
    assert analysis.metadata["extraction_message_count"] == 2
