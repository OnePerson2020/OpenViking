# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Generate a fixed Case Experience proposal directly from a Session."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openviking.config.vlm import VLMResolver
from openviking.message import Message
from openviking.server.identity import RequestContext
from openviking.session.memory.agent_experience_context_provider import (
    AgentExperienceContextProvider,
)
from openviking.session.memory.dataclass import MemoryFile, StoredLink
from openviking.session.memory.experience_dag import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    clip_evidence_summary,
    tool_evidence_summary,
)
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.utils.uri import generate_uri
from openviking.session.train.components.experience_improvement_gate import (
    EXPERIENCE_GATE_CONTEXTS_KEY,
)
from openviking.session.train.domain import ExperienceSet, RolloutAnalysis
from openviking.session.train.gradients import PatchSemanticGradient
from openviking.session.train.utils import safe_int
from openviking.storage.viking_fs import get_viking_fs
from openviking.telemetry import tracer


@dataclass(slots=True)
class ExperienceGradientContext:
    request_context: RequestContext
    messages: list[Message]
    strict_extract_errors: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    memory_registry: Any = None


@dataclass(slots=True)
class ExperienceGradientEstimator:
    viking_fs: Any = None
    vlm: Any = None
    vlm_resolver: VLMResolver | None = None

    @tracer("train.gradient_estimator.experience.estimate", ignore_result=True, ignore_args=True)
    async def estimate(
        self,
        analysis: RolloutAnalysis,
        experience_set: ExperienceSet,
        context: ExperienceGradientContext,
    ) -> list[PatchSemanticGradient]:
        if context is None or context.request_context is None:
            raise ValueError("ExperienceGradientContext.request_context is required")
        rollout = analysis.rollout
        if rollout is None:
            raise ValueError("Experience learning requires the source Session rollout and Case")
        session_uri = str(analysis.metadata.get("source_session_uri") or "")
        if not session_uri:
            raise ValueError("Experience learning requires a source Session archive URI")
        provider = AgentExperienceContextProvider(
            messages=rollout.messages,
            memory_registry=context.memory_registry,
            case=rollout.case,
            target_uri="",
            source_session_uri=session_uri,
            evaluation=analysis.evaluation,
            dag_execution=analysis.metadata.get("experience_execution"),
        )
        provider._ctx = context.request_context
        schema = provider._get_registry().get("experiences")
        if schema is None or not schema.enabled:
            return []
        target_uri = generate_uri(
            schema,
            {"experience_name": rollout.case.name},
            user_space=context.request_context.user.user_id,
        )
        if target_uri.rsplit("/", 1)[0] != experience_set.root_uri.rstrip("/"):
            raise ValueError("Case Experience target must be inside the Experience root")
        provider.target_uri = target_uri
        operations = await self._run_extract_loop(provider, context)
        if operations is None:
            return []
        if operations.errors:
            raise ValueError(f"Experience proposal failed: {operations.errors}")
        if operations.delete_file_contents or getattr(operations, "delete_replacements", {}):
            raise ValueError("Case Experience proposals cannot delete or replace targets")
        upserts = operations.upsert_operations
        if len(upserts) > 1:
            raise ValueError("A Session must propose at most one Experience for its Case")
        if not upserts:
            return []
        op = upserts[0]
        fields = dict(op.memory_fields or {})
        if (
            op.memory_type != "experiences"
            or fields.get("experience_name") != rollout.case.name
            or list(op.uris) != [target_uri]
            or fields.get("supersedes")
        ):
            raise ValueError("Experience proposal changed the fixed canonical Case target")
        if not str(fields.get("content") or "").strip():
            raise ValueError("Experience proposal has empty DAG content")
        old_file = op.old_memory_file_content
        after_file = MemoryFile(
            uri=target_uri,
            content=fields["content"],
            memory_type="experiences",
            extra_fields={
                **{k: v for k, v in fields.items() if k != "content"},
                "memory_type": "experiences",
                "case_name": rollout.case.name,
            },
        )
        current = next((p for p in experience_set.policies if p.uri == target_uri), None)
        evaluation = analysis.evaluation
        feedback = list(evaluation.feedback) if evaluation else []
        if evaluation:
            for criterion in evaluation.criterion_results:
                feedback.extend(criterion.feedback)
        gate_context = {
            "source_session_uri": session_uri,
            "case_name": rollout.case.name,
            "case_uri": analysis.metadata.get("case_uri", ""),
            "passed": evaluation.passed if evaluation else None,
            "score": evaluation.score if evaluation else None,
            "feedback": list(dict.fromkeys(feedback)),
            "experience_execution": dict(analysis.metadata.get("experience_execution") or {}),
            "evidence": _messages_to_gate_evidence(rollout.messages),
        }
        return [
            PatchSemanticGradient(
                before_file=old_file,
                after_file=after_file,
                base_version=(safe_int(old_file.extra_fields.get("version")) if old_file else None)
                or (current.version if current else None),
                rationale=f"Reflect on Session {session_uri} for canonical Case {rollout.case.name}.",
                links=[
                    StoredLink(
                        from_uri=target_uri,
                        to_uri=session_uri,
                        link_type="derived_from",
                        weight=1.0,
                    )
                ],
                confidence=0.7,
                metadata={
                    "memory_fields": fields,
                    "uris": [target_uri],
                    "case_name": rollout.case.name,
                    "case_uri": analysis.metadata.get("case_uri", ""),
                    "source_session_uri": session_uri,
                    "training_category": rollout.case.name,
                    EXPERIENCE_GATE_CONTEXTS_KEY: [gate_context],
                },
            )
        ]

    @tracer(
        "train.gradient_estimator.experience.extract_loop", ignore_result=True, ignore_args=True
    )
    async def _run_extract_loop(
        self, provider: AgentExperienceContextProvider, context: ExperienceGradientContext
    ):
        if self.vlm is None:
            if self.vlm_resolver is None:
                raise RuntimeError("ExperienceGradientEstimator requires a VLM resolver")
            vlm = await self.vlm_resolver.get_vlm(context.request_context.account_id)
            provider._vlm_config = vlm
        else:
            vlm = self.vlm
        viking_fs = self.viking_fs or get_viking_fs()
        if viking_fs is None:
            raise RuntimeError("VikingFS is required for experience gradient estimation")
        isolation = MemoryIsolationHandler(
            context.request_context,
            provider.get_extract_context(),
            allowed_memory_types={"experiences"},
        )
        isolation.prepare_messages()
        provider._isolation_handler = isolation
        provider._viking_fs = viking_fs
        orchestrator = ExtractLoop(
            vlm=vlm,
            viking_fs=viking_fs,
            ctx=context.request_context,
            context_provider=provider,
            isolation_handler=isolation,
            thinking=True,
        )
        operations, _ = await orchestrator.run()
        return operations


def _messages_to_gate_evidence(messages: list[Any]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for message_index, message in enumerate(messages):
        role = str(getattr(message, "role", "unknown") or "unknown")
        content = str(getattr(message, "content", "") or "")
        if content and not _is_training_control_message(content):
            evidence.append(
                {
                    "id": f"message:{message_index}",
                    "kind": f"{role}_message",
                    "summary": clip_evidence_summary(
                        f"{role}_message",
                        content,
                        MAX_EVIDENCE_SUMMARY_CHARS,
                    ),
                }
            )
        for part_index, part in enumerate(getattr(message, "parts", []) or []):
            tool_name = str(getattr(part, "tool_name", "") or "")
            tool_output = str(getattr(part, "tool_output", "") or "")
            if not tool_name or not tool_output:
                continue
            tool_input = getattr(part, "tool_input", None)
            summary = tool_evidence_summary(
                tool_name,
                tool_output,
                tool_input=tool_input,
                tool_status=getattr(part, "tool_status", None),
            )
            evidence.append(
                {
                    "id": f"message:{message_index}:tool:{part_index}",
                    "kind": "tool_result",
                    "summary": summary,
                }
            )
    return evidence[-256:]


def _is_training_control_message(content: str) -> bool:
    text = content.strip()
    return (
        text.startswith("# OpenViking Batch Training CaseSpec")
        or text.startswith("# OpenViking OutcomeEvaluation")
        or text.startswith("[Rollout Evaluation]")
    )
