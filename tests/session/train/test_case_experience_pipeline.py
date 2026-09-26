"""Local component integration: Session proposals -> merge -> replay -> storage."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_train_components import FakeVikingFS

from openviking.message import Message, TextPart
from openviking.server.identity import RequestContext, Role
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation, ResolvedOperations
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.train import (
    Case,
    ExperienceGradientContext,
    ExperienceGradientEstimator,
    ExperienceSetLoader,
    MemoryFilePolicyUpdater,
    PatchMergePolicyOptimizer,
    PatchMergePolicyOptimizerContext,
    PipelineContext,
    Rollout,
    Rubric,
    RubricEvaluation,
    SessionAnalyzerContext,
    SessionRolloutAnalyzer,
    StreamingPolicyTrainer,
    StreamingPolicyTrainerConfig,
)
from openviking.session.train.components.experience_improvement_gate import (
    ExperienceImprovementGate,
)
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig


@pytest.mark.asyncio
async def test_two_sessions_merge_one_case_experience_and_preserve_archives(monkeypatch):
    root = "viking://user/u/memories/experiences"
    uri = f"{root}/case_a.md"
    baseline = 'dag = workflow("report")\nknown = ask("reservation ID")\nreport = tell("report total")\nknown.then(report)'
    candidate = baseline.replace('tell("report total")', 'tell("report the verified total")')
    fs = FakeVikingFS(
        {
            uri: MemoryFileUtils.write(
                MemoryFile(
                    uri=uri,
                    content=baseline,
                    memory_type="experiences",
                    extra_fields={"experience_name": "case_a", "version": 3},
                )
            )
        }
    )
    lock = asyncio.Lock()

    async def acquire(*args, **kwargs):
        await lock.acquire()
        return "lease"

    async def release(lease):
        assert lease == "lease"
        lock.release()

    fs._async_agfs.pathlock_acquire_tree = acquire
    fs._async_agfs.pathlock_release = release
    ctx = RequestContext(user=UserIdentifier("default", "u"), role=Role.ROOT)
    policy_set = await ExperienceSetLoader(viking_fs=fs).load(root, ctx=ctx)
    case = Case("case_a", "report verified total", {}, Rubric("r", "report total", []))
    rollouts = []
    for index, passed in enumerate([True, False]):
        archive = f"viking://user/u/sessions/s{index}/history/archive_001"
        messages = [
            Message(id="request", role="user", parts=[TextPart(text="reservation A")]),
            Message(
                id="reply",
                role="assistant",
                parts=[TextPart(text="correct total" if passed else "wrong total")],
            ),
        ]
        fs.files[f"{archive}/messages.jsonl"] = "\n".join(json.dumps(m.to_dict()) for m in messages)
        rollouts.append(
            Rollout(
                case,
                messages,
                "snapshot-0",
                RubricEvaluation(passed, float(passed), [], [] if passed else ["wrong total"]),
                metadata={"source_session_uri": archive},
            )
        )
    archives_before = {
        path: content for path, content in fs.files.items() if path.endswith(".jsonl")
    }

    def output(old_file=None):
        return ResolvedOperations(
            upsert_operations=[
                ResolvedOperation(
                    memory_type="experiences",
                    uris=[uri],
                    old_memory_file_content=old_file,
                    memory_fields={"experience_name": case.name, "content": candidate},
                )
            ],
            delete_file_contents=[],
            errors=[],
        )

    async def propose(self, provider, context):
        assert provider.case is case
        return output(MemoryFileUtils.read(fs.files[uri], uri=uri))

    merges = []

    async def merge(self, *, gradients, policy_set, context):
        assert lock.locked()
        merges.append(gradients)
        assert policy_set.policies[0].content == baseline
        return output()

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", propose)
    monkeypatch.setattr(PatchMergePolicyOptimizer, "_run_merge_extract_loop", merge)

    class Decider:
        async def decide(self, instances, *, evidence, context):
            return {
                item.experience_uri: {"known": True, "report": "correct total" in context}
                for item in instances
            }

    async def judge(*, state, questions):
        return {qid: {"noul": 0.99} for qid in questions}

    gate = ExperienceImprovementGate(
        config=DagDeciderConfig(provider="jev"),
        dag_decider=Decider(),
        jev=SimpleNamespace(evaluate=AsyncMock(side_effect=judge)),
    )
    analyzer = SessionRolloutAnalyzer(viking_fs=fs)
    estimator = ExperienceGradientEstimator(viking_fs=fs)
    trainer = StreamingPolicyTrainer(
        policy_set=policy_set,
        rollout_analyzer=analyzer,
        gradient_estimator=estimator,
        policy_optimizer=PatchMergePolicyOptimizer(viking_fs=fs),
        policy_updater=MemoryFilePolicyUpdater(viking_fs=fs),
        context=PipelineContext(
            analysis_context=SessionAnalyzerContext(ctx),
            gradient_context=ExperienceGradientContext(ctx, []),
            optimization_context=PatchMergePolicyOptimizerContext(ctx),
            apply_context=ctx,
        ),
        config=StreamingPolicyTrainerConfig(max_gradients_per_update=2, max_wait_seconds=1),
    )
    try:
        analyses = [
            await analyzer.analyze(rollout, SessionAnalyzerContext(ctx)) for rollout in rollouts
        ]
        proposals = [
            await estimator.estimate(analysis, policy_set, ExperienceGradientContext(ctx, []))
            for analysis in analyses
        ]
        validated = [
            (await gate.validate_proposals(proposal, policy_set, None))[0] for proposal in proposals
        ]
        assert all(len(batch) == 1 for batch in validated)
        first, second = await asyncio.gather(
            *(
                trainer.submit_gradients(
                    gradients,
                    analysis=analysis,
                    rollout=rollout,
                )
                for gradients, analysis, rollout in zip(validated, analyses, rollouts, strict=True)
            )
        )
    finally:
        await trainer.close()
    assert first.batch_result is second.batch_result
    assert len(merges) == 1 and len(merges[0]) == 2
    assert first.apply_result.errors == second.apply_result.errors == []
    assert first.apply_result.written_uris == second.apply_result.written_uris == [uri]
    stored = MemoryFileUtils.read(fs.files[uri], uri=uri)
    assert stored.content == candidate
    assert len(stored.extra_fields["source_sessions"]) == 2
    assert {link["to_uri"] for link in stored.links} == {
        r.metadata["source_session_uri"] for r in rollouts
    }
    assert {
        path: content for path, content in fs.files.items() if path.endswith(".jsonl")
    } == archives_before
    assert not any("/trajectories/" in path for path in fs.files)
