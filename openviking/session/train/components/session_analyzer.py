# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Prepare Session evidence without generating intermediate Trajectory memories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openviking.config.vlm import VLMResolver
from openviking.message import Message
from openviking.server.identity import RequestContext
from openviking.session.memory import ExtractLoop
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperations, StoredLink
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.skill.session_skill_context_provider import (
    SESSION_SKILL_MEMORY_TYPE,
    SessionSkillContextProvider,
    load_skill_extract_registry,
)
from openviking.session.train.domain import Rollout, RolloutAnalysis
from openviking.session.train.gradients import PatchSemanticGradient
from openviking.session.train.interfaces import RolloutEvaluator
from openviking.storage.viking_fs import get_viking_fs
from openviking.telemetry import tracer


@dataclass(slots=True)
class SessionAnalyzerContext:
    request_context: RequestContext
    strict_extract_errors: bool = False
    evaluator_context: Any = None
    include_session_skills: bool = False
    source_archive_uri: str = ""


@dataclass(slots=True)
class SessionRolloutAnalyzer:
    viking_fs: Any = None
    vikingdb: Any = None
    vlm: Any = None
    evaluator: RolloutEvaluator | None = None
    vlm_resolver: VLMResolver | None = None

    @tracer("train.rollout_analyzer.session.analyze", ignore_result=True, ignore_args=True)
    async def analyze(self, rollout: Rollout, context: SessionAnalyzerContext) -> RolloutAnalysis:
        if context is None or context.request_context is None:
            raise ValueError("SessionAnalyzerContext.request_context is required")
        evaluation = rollout.evaluation
        if evaluation is None and self.evaluator is not None:
            evaluation = await self.evaluator.evaluate(rollout, context.evaluator_context)
        source_uri = str(rollout.metadata.get("source_session_uri") or context.source_archive_uri)
        gradients = []
        if context.include_session_skills:
            result = await self.extract_session_skills(
                messages=rollout.messages,
                ctx=context.request_context,
                source_archive_uri=source_uri,
                strict_extract_errors=context.strict_extract_errors,
            )
            gradients = result["skill_gradients"]
        return RolloutAnalysis(
            evaluation=evaluation,
            rollout=rollout,
            gradients=gradients,
            metadata={
                "case_uri": rollout.metadata.get("case_uri", ""),
                "source_session_uri": source_uri,
                "policy_snapshot_id": rollout.policy_snapshot_id,
                "experience_execution": _experience_execution_from_rollout(rollout),
            },
        )

    async def extract_session_skills(
        self,
        *,
        messages: list[Message],
        ctx: RequestContext,
        source_archive_uri: str = "",
        strict_extract_errors: bool = False,
    ) -> dict[str, Any]:
        vlm_config = None
        if self.vlm is None:
            if self.vlm_resolver is None:
                raise RuntimeError("Session skill extraction requires a VLM resolver")
            vlm_config = await self.vlm_resolver.get_vlm(ctx.account_id)
        provider = SessionSkillContextProvider(messages=messages, vlm_config=vlm_config)
        provider._registry = load_skill_extract_registry()
        provider.include_tool_parts_in_conversation = True
        provider._ctx = ctx
        viking_fs = self.viking_fs or get_viking_fs()
        provider._viking_fs = viking_fs
        isolation = MemoryIsolationHandler(
            ctx, provider.get_extract_context(), allowed_memory_types={SESSION_SKILL_MEMORY_TYPE}
        )
        isolation.prepare_messages()
        provider._isolation_handler = isolation
        orchestrator = ExtractLoop(
            vlm=self.vlm or vlm_config,
            viking_fs=viking_fs,
            ctx=ctx,
            context_provider=provider,
            isolation_handler=isolation,
            thinking=True,
        )
        operations, _ = await orchestrator.run()
        if operations is None:
            return {"skill_gradients": []}
        if operations.errors:
            raise ValueError(f"Session skill extraction failed: {operations.errors}")
        return {"skill_gradients": _skill_operations_to_gradients(operations)}


def _experience_execution_from_rollout(rollout: Rollout) -> dict[str, dict[str, Any]]:
    """Build the final per-Experience DAG snapshots recorded with a Session."""
    metadata = getattr(rollout, "metadata", {})
    precomputed = metadata.get("experience_execution")
    if isinstance(precomputed, dict):
        return {
            str(uri): dict(snapshot)
            for uri, snapshot in precomputed.items()
            if uri and isinstance(snapshot, dict)
        }
    return experience_execution_from_runtime(metadata.get("dag_runtime"))


def experience_execution_from_runtime(runtime: Any) -> dict[str, dict[str, Any]]:
    """Collapse runtime events into one factual final snapshot per Experience."""
    events = runtime.get("events") if isinstance(runtime, dict) else None
    if not isinstance(events, list):
        return {}

    snapshot_fields = (
        "state",
        "revision",
        "slot_values",
        "slot_evidence",
        "executed_nodes",
        "current_nodes",
        "waiting_for_context",
        "actions",
        "completed_nodes",
        "action_outcomes",
        "error",
    )
    snapshots: dict[str, dict[str, Any]] = {}
    node_slots_by_experience: dict[str, dict[int, str]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        experience_uri = str(event.get("experience_uri") or "").strip()
        if not experience_uri:
            continue
        snapshot = snapshots.setdefault(experience_uri, {})
        node_slots = node_slots_by_experience.setdefault(experience_uri, {})
        for node_id, slot_name in dict(event.get("node_slots") or {}).items():
            try:
                node_slots[int(node_id)] = str(slot_name)
            except (TypeError, ValueError):
                continue
        for collection_name in ("actions", "completed_nodes", "action_outcomes"):
            for node in event.get(collection_name) or []:
                if not isinstance(node, dict) or not node.get("slot_name"):
                    continue
                try:
                    node_slots[int(node.get("node_id"))] = str(node["slot_name"])
                except (TypeError, ValueError):
                    continue
        snapshot["experience_name"] = (
            experience_uri.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")
        )
        for field_name in snapshot_fields:
            if field_name in event:
                snapshot[field_name] = event[field_name]
    for experience_uri, snapshot in snapshots.items():
        node_slots = node_slots_by_experience.get(experience_uri, {})
        for field_name in ("executed_nodes", "current_nodes", "waiting_for_context"):
            node_ids = snapshot.get(field_name)
            if isinstance(node_ids, list):
                snapshot[field_name] = [node_slots.get(node_id, node_id) for node_id in node_ids]
    return snapshots


def _skill_operations_to_gradients(
    operations: ResolvedOperations,
    *,
    viking_fs: Any = None,
    ctx: Any = None,
) -> list[PatchSemanticGradient]:
    """Convert skill ResolvedOperations to PatchSemanticGradient instances.

    The resulting gradients carry the full proposed skill content in their
    ``after_file`` so the patch-merge optimizer can reconcile multiple
    proposals against the current policy set.
    """
    gradients: list[PatchSemanticGradient] = []
    for op in operations.upsert_operations or []:
        if op.memory_type != SESSION_SKILL_MEMORY_TYPE:
            continue
        fields = dict(op.memory_fields or {})
        skill_name = str(fields.get("skill_name") or _fallback_skill_name(op))
        target_uri = (op.uris or [None])[0]
        after_content = str(fields.get("content") or "")
        if not after_content.strip():
            continue

        old_file = op.old_memory_file_content
        after_file = MemoryFile(
            uri=target_uri,
            content=after_content,
            memory_type="skills",
            extra_fields={
                **dict(getattr(old_file, "extra_fields", {}) or {}),
                **{k: v for k, v in fields.items() if k != "content"},
                "memory_type": "skills",
                "skill_name": skill_name,
            },
        )
        links: list[StoredLink] = []

        gradients.append(
            PatchSemanticGradient(
                before_file=old_file,
                after_file=after_file,
                base_version=_base_version_from_old_file(old_file),
                rationale=(
                    "Session skill patch extracted from Session by SessionSkillContextProvider."
                ),
                links=links,
                confidence=0.7,
                metadata={
                    "source": "session_skill_extract",
                    "memory_fields": fields,
                    "skill_name": skill_name,
                    "uris": list(op.uris or []),
                },
            )
        )
    return gradients


def _fallback_skill_name(op: Any) -> str:
    uris = getattr(op, "uris", None) or []
    if uris:
        uri = str(uris[0])
        # path/to/skills/my_skill/SKILL.md → my_skill
        parts = uri.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-1] == "SKILL.md":
            return parts[-2]
        return parts[-1].removesuffix(".md")
    return "unknown_skill"


def _base_version_from_old_file(old_file: Any) -> int | None:
    if old_file is None:
        return None
    fields = getattr(old_file, "extra_fields", {}) or {}
    try:
        v = int(fields.get("version"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None
