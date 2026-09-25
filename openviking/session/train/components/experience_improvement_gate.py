# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Validate candidate Experience updates by replaying their DAGs with Jev."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openviking.models.jev import JevClient
from openviking.service.experience_dag_decider import ExperienceDagDecider
from openviking.session.memory.experience_dag import Dag, DagAction, DagEvidenceRef, DagInstance
from openviking.session.memory.experience_dag_compiler import compile_dag
from openviking.session.train.domain import PolicySet, PolicyUpdatePlan
from openviking.session.train.interfaces import SemanticGradient
from openviking.telemetry import tracer
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig
from openviking_cli.utils.config.jev_config import JevConfig

EXPERIENCE_GATE_CONTEXTS_KEY = "_experience_gate_contexts"
_MAX_REPLAY_ROUNDS = 32


@dataclass(slots=True)
class ExperienceImprovementGate:
    """Keep only Experience plans that improve their source trajectories."""

    config: DagDeciderConfig
    jev_config: JevConfig | None = None
    dag_decider: Any = None
    jev: Any = None

    def __post_init__(self) -> None:
        if self.jev is None and self.jev_config is not None:
            self.jev = JevClient(self.jev_config)
        if self.dag_decider is None and self.jev is not None:
            self.dag_decider = ExperienceDagDecider(config=self.config, jev=self.jev)

    @tracer("train.policy_gate.experience.validate", ignore_args=True, ignore_result=True)
    async def validate(
        self,
        plan: PolicyUpdatePlan,
        gradients: list[SemanticGradient],
        policy_set: PolicySet,
        context: Any,
    ) -> PolicyUpdatePlan:
        del policy_set, context
        upserts = [
            item
            for item in plan.items
            if item.memory_type == "experiences" and item.kind == "upsert" and item.after_content
        ]
        if not upserts:
            return plan

        if self.dag_decider is None or self.jev is None:
            return PolicyUpdatePlan(
                items=[],
                metadata={
                    **dict(plan.metadata or {}),
                    "experience_improvement_gate": {
                        "passed": False,
                        "atomic": False,
                        "accepted_count": 0,
                        "rejected_count": len(upserts),
                        "candidates": [],
                        "reason": "shared Jev configuration is missing",
                    },
                },
            )

        contexts_by_trajectory = _gate_contexts_by_trajectory(gradients)
        diagnostics: list[dict[str, Any]] = []
        for item in upserts:
            trajectory_uris = {
                link.to_uri
                for link in item.links
                if link.link_type == "derived_from" and link.to_uri
            }
            validation_contexts = [
                gate_context
                for trajectory_uri in trajectory_uris
                for gate_context in contexts_by_trajectory.get(trajectory_uri, [])
            ]
            if not validation_contexts and len(contexts_by_trajectory) == 1:
                validation_contexts = next(iter(contexts_by_trajectory.values()))

            item_diagnostics: list[dict[str, Any]] = []
            if not validation_contexts:
                item_diagnostics.append(
                    {
                        "passed": False,
                        "reason": "candidate has no source trajectory validation context",
                    }
                )
            else:
                for gate_context in validation_contexts:
                    item_diagnostics.append(
                        await self._validate_candidate(
                            source=str(item.after_content),
                            experience_uri=item.target_uri or item.target_name,
                            gate_context=gate_context,
                        )
                    )
            diagnostics.append(
                {
                    "target_uri": item.target_uri,
                    "target_name": item.target_name,
                    "passed": bool(item_diagnostics)
                    and all(result.get("passed") for result in item_diagnostics),
                    "replays": item_diagnostics,
                }
            )

        passed = bool(diagnostics) and all(item["passed"] for item in diagnostics)
        accepted_upserts = {
            _plan_item_identity(item)
            for item, diagnostic in zip(upserts, diagnostics, strict=True)
            if diagnostic["passed"]
        }
        accepted_items = [
            item
            for item in plan.items
            if _keep_plan_item(
                item,
                accepted_upserts=accepted_upserts,
                all_upserts_passed=passed,
                candidate_upserts=upserts,
            )
        ]
        tracer.set("experience.gate.candidate_count", len(diagnostics))
        tracer.set("experience.gate.accepted_count", len(accepted_upserts))
        tracer.set("experience.gate.passed", passed)
        metadata = {
            **dict(plan.metadata or {}),
            "experience_improvement_gate": {
                "passed": passed,
                "atomic": False,
                "accepted_count": len(accepted_upserts),
                "rejected_count": len(diagnostics) - len(accepted_upserts),
                "candidates": diagnostics,
            },
        }
        return PolicyUpdatePlan(items=accepted_items, metadata=metadata)

    async def _validate_candidate(
        self,
        *,
        source: str,
        experience_uri: str,
        gate_context: dict[str, Any],
    ) -> dict[str, Any]:
        trajectory_uri = str(gate_context.get("trajectory_uri") or "")
        try:
            dag = Dag.model_validate_json(compile_dag(source))
        except Exception as exc:
            return {
                "trajectory_uri": trajectory_uri,
                "passed": False,
                "reason": f"candidate DAG does not compile: {exc}",
            }

        evidence = _parse_evidence(gate_context.get("evidence"))
        context = json.dumps(
            [item.model_dump(mode="json") for item in evidence],
            ensure_ascii=False,
        )
        instance = DagInstance(
            experience_uri=experience_uri,
            instance_id="improvement-gate",
            dag=dag,
        )
        actions, waiting = instance.advance()
        seen: set[str] = set()
        try:
            for _ in range(min(_MAX_REPLAY_ROUNDS, len(dag.nodes) + 1)):
                signature = _instance_signature(instance)
                if instance.state == "completed" or signature in seen:
                    break
                seen.add(signature)
                values = await self.dag_decider.decide(
                    [instance],
                    evidence=evidence,
                    context=context,
                )
                slot_values = values.get(experience_uri, {})
                changed_values = {
                    name: value
                    for name, value in slot_values.items()
                    if instance.slot_values.get(name) != value
                }
                if not changed_values:
                    break
                instance.merge_slot_values(changed_values)
                actions, waiting = instance.advance()
        except Exception as exc:
            return {
                "trajectory_uri": trajectory_uri,
                "passed": False,
                "reason": f"candidate DAG replay failed: {exc}",
            }

        replay = _replay_summary(instance, actions=actions, waiting=waiting)
        trajectory_passed = bool(gate_context.get("passed"))
        rollout_passed = bool(gate_context.get("rollout_passed", trajectory_passed))
        if trajectory_passed and instance.state == "completed":
            return {
                "trajectory_uri": trajectory_uri,
                "passed": True,
                "reason": "candidate preserves the successful trajectory",
                "replay": replay,
            }

        if not trajectory_passed and instance.state == "completed":
            return {
                "trajectory_uri": trajectory_uri,
                "passed": False,
                "reason": "candidate still accepts the failed trajectory as complete",
                "replay": replay,
            }
        if not actions:
            return {
                "trajectory_uri": trajectory_uri,
                "passed": False,
                "reason": "candidate detects an incomplete path but emits no corrective action",
                "replay": replay,
            }

        if trajectory_passed and rollout_passed:
            return {
                "trajectory_uri": trajectory_uri,
                "passed": False,
                "reason": "candidate no longer completes for a successful rollout",
                "replay": replay,
            }

        try:
            relevance = await self._judge_corrective_actions(
                source=source,
                actions=actions,
                replay=replay,
                gate_context=gate_context,
            )
        except Exception as exc:
            return {
                "trajectory_uri": trajectory_uri,
                "passed": False,
                "reason": f"candidate corrective-action validation failed: {exc}",
                "replay": replay,
            }
        relevant = relevance >= self.config.noul_true_threshold
        reattributed = trajectory_passed and not rollout_passed
        return {
            "trajectory_uri": trajectory_uri,
            "passed": relevant,
            "reason": (
                "candidate correction is attributable to the failed rollout"
                if relevant and reattributed
                else "candidate catches the failed trajectory with a relevant corrective action"
                if relevant
                else "candidate corrective action does not address the evaluation failure"
            ),
            "corrective_action_score": relevance,
            "failure_reattributed": reattributed and relevant,
            "replay": replay,
        }

    async def _judge_corrective_actions(
        self,
        *,
        source: str,
        actions: list[DagAction],
        replay: dict[str, Any],
        gate_context: dict[str, Any],
    ) -> float:
        state = {
            "trajectory": _clip(str(gate_context.get("trajectory_summary") or ""), 6000),
            "evaluation_feedback": _clip(
                json.dumps(gate_context.get("feedback") or [], ensure_ascii=False),
                8000,
            ),
            "actual_experience_execution": _clip(
                json.dumps(
                    gate_context.get("experience_execution") or {},
                    ensure_ascii=False,
                    default=str,
                ),
                6000,
            ),
            "candidate_source": _clip(source, 8000),
            "candidate_replay": replay,
            "current_actions": [action.model_dump(mode="json") for action in actions],
        }
        answers = await self.jev.evaluate(
            state=state,
            questions={
                "improvement_effective": {
                    "type": "noul",
                    "instructions": (
                        "Do the candidate Experience's current actionable instructions directly "
                        "address the recorded evaluation failure, so following them would prevent "
                        "the same failure? Be strict: a generic, unrelated, or already-satisfied "
                        "instruction is false."
                    ),
                    "criteria": {
                        "true": "The action directly addresses the concrete failed requirement.",
                        "false": "The action is generic, unrelated, or would allow the same failure.",
                    },
                }
            },
        )
        answer = answers.get("improvement_effective")
        score = answer.get("noul") if isinstance(answer, dict) else None
        return (
            float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0
        )


def _gate_contexts_by_trajectory(
    gradients: list[SemanticGradient],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for gradient in gradients:
        metadata = dict(getattr(gradient, "metadata", {}) or {})
        contexts = metadata.get(EXPERIENCE_GATE_CONTEXTS_KEY) or []
        for gate_context in contexts:
            if not isinstance(gate_context, dict):
                continue
            trajectory_uri = str(gate_context.get("trajectory_uri") or "")
            if trajectory_uri:
                result.setdefault(trajectory_uri, []).append(gate_context)
    return result


def _plan_item_identity(item: Any) -> str:
    return str(item.target_uri or item.target_name or "")


def _keep_plan_item(
    item: Any,
    *,
    accepted_upserts: set[str],
    all_upserts_passed: bool,
    candidate_upserts: list[Any],
) -> bool:
    if item.memory_type != "experiences":
        return True
    if item.kind == "upsert":
        return _plan_item_identity(item) in accepted_upserts
    if item.kind != "delete":
        return False
    superseded_by = {str(value) for value in item.metadata.get("superseded_by", []) if value}
    if superseded_by:
        return bool(superseded_by & accepted_upserts)
    # A merge-only delete cannot be attributed to one candidate. Preserve it only
    # when every candidate in this merged plan passed its own replay.
    return all_upserts_passed and bool(candidate_upserts)


def _parse_evidence(value: Any) -> list[DagEvidenceRef]:
    evidence: list[DagEvidenceRef] = []
    for item in value if isinstance(value, list) else []:
        try:
            evidence.append(DagEvidenceRef.model_validate(item))
        except Exception:
            continue
    return evidence[:256]


def _instance_signature(instance: DagInstance) -> str:
    return json.dumps(
        {
            "state": instance.state,
            "executed_nodes": instance.executed_nodes,
            "current_nodes": instance.current_nodes,
            "slot_values": instance.slot_values,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _replay_summary(
    instance: DagInstance,
    *,
    actions: list[DagAction],
    waiting: list[int],
) -> dict[str, Any]:
    node_slots = {node_id: node.slot_name for node_id, node in instance.dag.nodes.items()}
    return {
        "state": instance.state,
        "executed_nodes": [node_slots[node_id] for node_id in instance.executed_nodes],
        "current_nodes": [node_slots[node_id] for node_id in instance.current_nodes],
        "waiting_for_context": [node_slots[node_id] for node_id in waiting],
        "actions": [
            {
                "slot_name": action.slot_name,
                "description": action.description,
                "provider": action.provider.model_dump(mode="json"),
            }
            for action in actions
        ],
        "slot_values": dict(instance.slot_values),
    }


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, (limit - 32) // 2)
    return value[:half] + "\n...[truncated]...\n" + value[-half:]
