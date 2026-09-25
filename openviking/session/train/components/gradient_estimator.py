# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""ExtractLoop-backed GradientEstimator component."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from openviking.config.vlm import VLMResolver
from openviking.message import Message
from openviking.server.identity import RequestContext
from openviking.session.memory.agent_experience_context_provider import (
    AgentExperienceContextProvider,
)
from openviking.session.memory.dataclass import MemoryFile, StoredLink
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.train.components.experience_improvement_gate import (
    EXPERIENCE_GATE_CONTEXTS_KEY,
)
from openviking.session.train.domain import ExperienceSet, RolloutAnalysis, Trajectory
from openviking.session.train.gradients import PatchSemanticGradient
from openviking.session.train.utils import first_uri, safe_int
from openviking.storage.viking_fs import get_viking_fs
from openviking.telemetry import tracer
from openviking_cli.utils import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ExperienceGradientContext:
    """Context for ExperienceGradientEstimator."""

    request_context: RequestContext
    messages: list[Message]
    strict_extract_errors: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExperienceGradientEstimator:
    """Estimate PatchSemanticGradients via experience ExtractLoop.

    This component reuses AgentExperienceContextProvider and ExtractLoop but stops
    before MemoryUpdater.apply_operations.  The resolved operations are converted
    into PatchSemanticGradient instances.
    """

    viking_fs: Any = None
    vlm: Any = None
    vlm_resolver: VLMResolver | None = None

    @tracer(
        "train.gradient_estimator.experience.estimate",
        ignore_result=True,
        ignore_args=True,
    )
    async def estimate(
        self,
        analysis: RolloutAnalysis,
        experience_set: ExperienceSet,
        context: ExperienceGradientContext,
    ) -> list[PatchSemanticGradient]:
        if context is None or context.request_context is None:
            raise ValueError("ExperienceGradientContext.request_context is required")

        extract_context = _context_with_analysis_messages(context, analysis)

        async def estimate_one(trajectory: Trajectory) -> list[PatchSemanticGradient]:
            try:
                operations = await self._run_extract_loop(trajectory, extract_context)
            except Exception:
                logger.exception("Experience gradient estimation failed")
                if context.strict_extract_errors:
                    raise
                return []
            if operations is None:
                return []
            gradients = _operations_to_gradients(
                operations=operations,
                trajectory=trajectory,
                analysis=analysis,
                experience_set=experience_set,
            )
            return _select_primary_gradient(gradients, trajectory)

        gradient_batches = await asyncio.gather(
            *(estimate_one(trajectory) for trajectory in analysis.trajectories)
        )
        gradients = [gradient for batch in gradient_batches for gradient in batch]
        return _coalesce_gradients(gradients)

    @tracer(
        "train.gradient_estimator.experience.extract_loop",
        ignore_result=True,
        ignore_args=True,
    )
    async def _run_extract_loop(
        self,
        trajectory: Trajectory,
        context: ExperienceGradientContext,
    ):
        vlm_config = None
        if self.vlm is None:
            if self.vlm_resolver is None:
                raise RuntimeError(
                    "ExperienceGradientEstimator requires a VLM resolver for account-owned work"
                )
            vlm_config = await self.vlm_resolver.get_vlm(context.request_context.account_id)
            vlm = vlm_config
        else:
            vlm = self.vlm
        viking_fs = self.viking_fs or get_viking_fs()
        if viking_fs is None:
            raise RuntimeError("VikingFS is required for experience gradient estimation")

        dag_execution = _dag_execution_feedback(analysis=context.metadata.get("analysis"))
        provider_kwargs = {
            "messages": context.messages,
            "trajectory_summary": trajectory.content,
            "trajectory_uri": trajectory.uri,
        }
        if vlm_config is not None:
            provider_kwargs["vlm_config"] = vlm_config
        if dag_execution is not None:
            provider_kwargs["dag_execution"] = dag_execution
        provider = AgentExperienceContextProvider(**provider_kwargs)
        if hasattr(provider, "get_extract_context"):
            extract_context = provider.get_extract_context()
        else:
            extract_context = context
        isolation_handler = MemoryIsolationHandler(
            context.request_context,
            extract_context,
            allowed_memory_types={"experiences"},
        )
        isolation_handler.prepare_messages()

        provider._isolation_handler = isolation_handler
        provider._ctx = context.request_context
        provider._viking_fs = viking_fs

        orchestrator = ExtractLoop(
            vlm=vlm,
            viking_fs=viking_fs,
            ctx=context.request_context,
            context_provider=provider,
            isolation_handler=isolation_handler,
            thinking=True,
        )
        operations, _ = await orchestrator.run()
        return operations


def _context_with_analysis_messages(
    context: ExperienceGradientContext,
    analysis: RolloutAnalysis,
) -> ExperienceGradientContext:
    messages = analysis.metadata.get("rollout_messages")
    return ExperienceGradientContext(
        request_context=context.request_context,
        messages=list(messages) if messages else context.messages,
        strict_extract_errors=context.strict_extract_errors,
        metadata={**context.metadata, "analysis": analysis},
    )


def _dag_execution_feedback(analysis: RolloutAnalysis | None) -> dict[str, Any] | None:
    if analysis is None:
        return None
    execution = analysis.metadata.get("experience_execution")
    if not isinstance(execution, dict) or not execution:
        return None
    return {
        "outcome": {
            "passed": analysis.evaluation.passed,
            "score": analysis.evaluation.score,
            "feedback": analysis.evaluation.feedback,
            "criteria": [
                {
                    "criterion_name": criterion.criterion_name,
                    "passed": criterion.passed,
                    "score": criterion.score,
                    "feedback": criterion.feedback,
                    "evidence": criterion.evidence,
                }
                for criterion in analysis.evaluation.criterion_results
            ],
        },
        "execution": execution,
    }


def _operations_to_gradients(
    *,
    operations: Any,
    trajectory: Trajectory,
    analysis: RolloutAnalysis,
    experience_set: ExperienceSet,
) -> list[PatchSemanticGradient]:
    gradients: list[PatchSemanticGradient] = []
    for op in getattr(operations, "upsert_operations", []) or []:
        if getattr(op, "memory_type", None) != "experiences":
            continue
        fields = dict(getattr(op, "memory_fields", {}) or {})
        after_content = str(fields.get("content") or "")
        if not after_content.strip():
            continue

        old_file = getattr(op, "old_memory_file_content", None)
        target_name = str(fields.get("experience_name") or _fallback_experience_name(op))
        target_uri = first_uri(getattr(op, "uris", []) or [])
        base_version = _base_version(old_file, target_uri, experience_set)
        after_file = _operation_after_file(
            fields=fields,
            target_name=target_name,
            target_uri=target_uri,
            old_file=old_file,
        )

        gradients.append(
            PatchSemanticGradient(
                before_file=old_file,
                after_file=after_file,
                base_version=base_version,
                rationale=(
                    "ExtractLoop proposed an experience content update "
                    f"from trajectory {trajectory.uri}."
                ),
                links=[
                    StoredLink(
                        from_uri=target_uri or "",
                        to_uri=trajectory.uri,
                        link_type="derived_from",
                        weight=1.0,
                        match_text=None,
                        description="",
                    )
                ],
                confidence=_confidence(trajectory, analysis),
                metadata={
                    "memory_fields": fields,
                    "uris": list(getattr(op, "uris", []) or []),
                    "trajectory_outcome": trajectory.outcome,
                    "rubric_passed": analysis.evaluation.passed,
                    "supersedes": fields.get("supersedes"),
                    "training_category": _trajectory_training_category(trajectory, analysis),
                    EXPERIENCE_GATE_CONTEXTS_KEY: [
                        _experience_gate_context(trajectory=trajectory, analysis=analysis)
                    ],
                },
            )
        )
    return gradients


def _select_primary_gradient(
    gradients: list[PatchSemanticGradient],
    trajectory: Trajectory,
) -> list[PatchSemanticGradient]:
    """Keep one Experience proposal for an already intent-scoped trajectory."""
    if len(gradients) <= 1:
        return gradients

    trajectory_name = _normalized_name(trajectory.name)
    selected = max(
        enumerate(gradients),
        key=lambda item: (
            SequenceMatcher(
                None,
                trajectory_name,
                _normalized_name(item[1].target_name),
            ).ratio(),
            item[1].confidence,
            len(item[1].after_file.content),
            -item[0],
        ),
    )[1]
    logger.warning(
        "Experience extraction returned %d entries for trajectory %s; keeping primary %s",
        len(gradients),
        trajectory.uri,
        selected.target_name,
    )
    return [selected]


def _coalesce_gradients(
    gradients: list[PatchSemanticGradient],
) -> list[PatchSemanticGradient]:
    """Collapse duplicate targets while retaining all trajectory provenance links."""
    by_target: dict[str, PatchSemanticGradient] = {}
    order: list[str] = []
    for gradient in gradients:
        key = gradient.target_uri or _normalized_name(gradient.target_name)
        existing = by_target.get(key)
        if existing is None:
            by_target[key] = gradient
            order.append(key)
            continue

        preferred, other = (
            max(
                (existing, gradient),
                key=lambda item: (item.confidence, len(item.after_file.content)),
            ),
            min(
                (existing, gradient),
                key=lambda item: (item.confidence, len(item.after_file.content)),
            ),
        )
        preferred.links = _dedupe_links([*existing.links, *gradient.links])
        source_uris = {
            link.to_uri
            for link in preferred.links
            if link.link_type == "derived_from" and link.to_uri
        }
        preferred.metadata = {
            **other.metadata,
            **preferred.metadata,
            "source_trajectory_uris": sorted(source_uris),
            EXPERIENCE_GATE_CONTEXTS_KEY: _merge_gate_contexts(existing, gradient),
        }
        by_target[key] = preferred
    return [by_target[key] for key in order]


def _dedupe_links(links: list[StoredLink]) -> list[StoredLink]:
    deduped: list[StoredLink] = []
    seen: set[tuple[str, str, str]] = set()
    for link in links:
        key = (link.from_uri, link.to_uri, link.link_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(link)
    return deduped


def _normalized_name(value: str) -> str:
    return "".join(char for char in str(value).casefold() if char.isalnum())


def _experience_gate_context(
    *,
    trajectory: Trajectory,
    analysis: RolloutAnalysis,
) -> dict[str, Any]:
    feedback = list(analysis.evaluation.feedback)
    for criterion in analysis.evaluation.criterion_results:
        feedback.extend(criterion.feedback)
    return {
        "trajectory_uri": trajectory.uri,
        "trajectory_summary": trajectory.content,
        "passed": _trajectory_passed_for_gate(trajectory, analysis),
        "trajectory_outcome": str(trajectory.outcome),
        "rollout_passed": analysis.evaluation.passed,
        "score": analysis.evaluation.score,
        "feedback": list(dict.fromkeys(str(item) for item in feedback if item)),
        "experience_execution": dict(analysis.metadata.get("experience_execution") or {}),
        "evidence": _messages_to_gate_evidence(analysis.metadata.get("rollout_messages") or []),
    }


def _trajectory_passed_for_gate(
    trajectory: Trajectory,
    analysis: RolloutAnalysis,
) -> bool:
    outcome = str(trajectory.outcome).strip().lower()
    if outcome == "success":
        return True
    if outcome in {"failure", "partial", "unfinished"}:
        return False
    return analysis.evaluation.passed


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
                    "summary": content[:4096],
                }
            )
        for part_index, part in enumerate(getattr(message, "parts", []) or []):
            tool_name = str(getattr(part, "tool_name", "") or "")
            tool_output = str(getattr(part, "tool_output", "") or "")
            if not tool_name or not tool_output:
                continue
            tool_input = getattr(part, "tool_input", None)
            summary = json.dumps(
                {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_output": tool_output,
                    "tool_status": getattr(part, "tool_status", None),
                },
                ensure_ascii=False,
                default=str,
            )
            evidence.append(
                {
                    "id": f"message:{message_index}:tool:{part_index}",
                    "kind": "tool_result",
                    "summary": summary[:4096],
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


def _merge_gate_contexts(
    first: PatchSemanticGradient,
    second: PatchSemanticGradient,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gradient in (first, second):
        values = gradient.metadata.get(EXPERIENCE_GATE_CONTEXTS_KEY) or []
        for context in values:
            if not isinstance(context, dict):
                continue
            trajectory_uri = str(context.get("trajectory_uri") or "")
            if not trajectory_uri or trajectory_uri in seen:
                continue
            seen.add(trajectory_uri)
            contexts.append(context)
    return contexts


def _trajectory_training_category(
    trajectory: Trajectory,
    analysis: RolloutAnalysis,
) -> str:
    trajectory_metadata = dict(getattr(trajectory, "metadata", {}) or {})
    for key in ("training_category", "category"):
        value = trajectory_metadata.get(key)
        if value:
            return str(value)

    analysis_metadata = dict(getattr(analysis, "metadata", {}) or {})
    for key in ("training_category", "category", "case_task_signature", "task_signature"):
        value = analysis_metadata.get(key)
        if value:
            return str(value)

    if trajectory.retrieval_anchor:
        return str(trajectory.retrieval_anchor)
    return str(trajectory.name)


def _operation_after_file(
    *,
    fields: dict[str, Any],
    target_name: str,
    target_uri: str | None,
    old_file: MemoryFile | None,
) -> MemoryFile:
    extra_fields = dict(getattr(old_file, "extra_fields", {}) or {})
    for key, value in fields.items():
        if key != "content":
            extra_fields[key] = value
    extra_fields["memory_type"] = "experiences"
    extra_fields["experience_name"] = target_name
    return MemoryFile(
        uri=target_uri,
        content=str(fields.get("content") or ""),
        links=list(getattr(old_file, "links", []) or []),
        backlinks=list(getattr(old_file, "backlinks", []) or []),
        memory_type="experiences",
        extra_fields=extra_fields,
    )


def _fallback_experience_name(op: Any) -> str:
    uri = first_uri(getattr(op, "uris", []) or [])
    if uri:
        return uri.rstrip("/").split("/")[-1].removesuffix(".md")
    return "unknown_experience"


def _base_version(
    old_file: Any, target_uri: str | None, experience_set: ExperienceSet
) -> int | None:
    if old_file is not None:
        fields = getattr(old_file, "extra_fields", {}) or {}
        version = safe_int(fields.get("version"))
        if version is not None:
            return version
    if target_uri:
        for policy in experience_set.policies:
            if policy.uri == target_uri:
                return policy.version
    return None


def _confidence(trajectory: Trajectory, analysis: RolloutAnalysis) -> float:
    confidence = 0.5
    if analysis.evaluation.passed:
        confidence += 0.2
    outcome = str(trajectory.outcome).lower()
    if outcome == "success":
        confidence += 0.2
    elif outcome in {"failure", "partial"}:
        confidence -= 0.2
    elif outcome == "unfinished":
        confidence -= 0.1
    return max(0.0, min(1.0, confidence))
