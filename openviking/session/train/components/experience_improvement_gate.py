# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Validate candidate Experience updates by replaying their DAGs with Jev."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
class _CandidateValidation:
    source: str
    baseline_source: str | None
    experience_uri: str
    gate_context: dict[str, Any]
    ordinal: int
    result: dict[str, Any] | None = None


@dataclass(slots=True)
class _DagReplayRequest:
    key: str
    source: str


@dataclass(slots=True)
class _DagReplayResult:
    replay: dict[str, Any] | None = None
    actions: list[DagAction] = field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None


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
        validations_by_item: list[list[_CandidateValidation]] = []
        batches: dict[str, list[_CandidateValidation]] = {}
        ordinal = 0
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

            item_validations: list[_CandidateValidation] = []
            if not validation_contexts:
                validation = _CandidateValidation(
                    source=str(item.after_content),
                    baseline_source=item.before_content,
                    experience_uri=item.target_uri or item.target_name,
                    gate_context={},
                    ordinal=ordinal,
                    result={
                        "passed": False,
                        "reason": "candidate has no source trajectory validation context",
                    },
                )
                ordinal += 1
                item_validations.append(validation)
            else:
                for gate_context in validation_contexts:
                    validation = _CandidateValidation(
                        source=str(item.after_content),
                        baseline_source=item.before_content,
                        experience_uri=item.target_uri or item.target_name,
                        gate_context=gate_context,
                        ordinal=ordinal,
                    )
                    ordinal += 1
                    item_validations.append(validation)
                    batches.setdefault(_gate_context_key(gate_context), []).append(validation)
            validations_by_item.append(item_validations)

        for batch in batches.values():
            await self._validate_batch(batch)

        diagnostics: list[dict[str, Any]] = []
        for item, item_validations in zip(upserts, validations_by_item, strict=True):
            item_diagnostics = [
                validation.result
                or {
                    "passed": False,
                    "reason": "candidate validation did not produce a result",
                }
                for validation in item_validations
            ]
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

    async def _validate_batch(self, validations: list[_CandidateValidation]) -> None:
        gate_context = validations[0].gate_context
        replay_requests: list[_DagReplayRequest] = []
        for validation in validations:
            replay_requests.append(
                _DagReplayRequest(
                    key=_replay_key(validation, "candidate"),
                    source=validation.source,
                )
            )
            if validation.baseline_source is not None:
                replay_requests.append(
                    _DagReplayRequest(
                        key=_replay_key(validation, "baseline"),
                        source=validation.baseline_source,
                    )
                )

        replay_results = await self._replay_dags(replay_requests, gate_context=gate_context)
        needs_judgement: list[tuple[_CandidateValidation, _DagReplayResult]] = []
        for validation in validations:
            candidate = replay_results[_replay_key(validation, "candidate")]
            baseline = (
                replay_results.get(_replay_key(validation, "baseline"))
                if validation.baseline_source is not None
                else None
            )
            validation.result = _preliminary_validation_result(
                validation,
                candidate=candidate,
                baseline=baseline,
            )
            if validation.result is None:
                needs_judgement.append((validation, candidate))

        if not needs_judgement:
            return
        try:
            scores = await self._judge_corrective_actions_batch(
                needs_judgement,
                replay_results=replay_results,
            )
        except Exception as exc:
            for validation, candidate in needs_judgement:
                validation.result = {
                    "trajectory_uri": str(validation.gate_context.get("trajectory_uri") or ""),
                    "passed": False,
                    "reason": f"candidate corrective-action validation failed: {exc}",
                    "replay": candidate.replay,
                    **_baseline_diagnostic(
                        validation,
                        replay_results.get(_replay_key(validation, "baseline")),
                    ),
                }
            return

        for validation, candidate in needs_judgement:
            relevance = scores.get(validation.ordinal, 0.0)
            relevant = relevance >= self.config.noul_true_threshold
            gate_context = validation.gate_context
            trajectory_passed = bool(gate_context.get("passed"))
            rollout_passed = bool(gate_context.get("rollout_passed", trajectory_passed))
            reattributed = trajectory_passed and not rollout_passed
            compared = validation.baseline_source is not None
            validation.result = {
                "trajectory_uri": str(gate_context.get("trajectory_uri") or ""),
                "passed": relevant,
                "reason": (
                    "candidate improves on the baseline for the failed rollout"
                    if relevant and compared
                    else "candidate correction is attributable to the failed rollout"
                    if relevant and reattributed
                    else "candidate catches the failed trajectory with a relevant corrective action"
                    if relevant
                    else "candidate does not improve on the baseline for the evaluation failure"
                    if compared
                    else "candidate corrective action does not address the evaluation failure"
                ),
                "corrective_action_score": relevance,
                "failure_reattributed": reattributed and relevant,
                "replay": candidate.replay,
                **_baseline_diagnostic(
                    validation,
                    replay_results.get(_replay_key(validation, "baseline")),
                ),
            }

    async def _replay_dags(
        self,
        requests: list[_DagReplayRequest],
        *,
        gate_context: dict[str, Any],
    ) -> dict[str, _DagReplayResult]:
        results: dict[str, _DagReplayResult] = {}
        instances: dict[str, DagInstance] = {}
        actions_by_key: dict[str, list[DagAction]] = {}
        waiting_by_key: dict[str, list[int]] = {}
        for request in requests:
            try:
                dag = Dag.model_validate_json(compile_dag(request.source))
                instance = DagInstance(
                    experience_uri=request.key,
                    instance_id="improvement-gate",
                    dag=dag,
                )
                actions_by_key[request.key], waiting_by_key[request.key] = instance.advance()
                instances[request.key] = instance
            except Exception as exc:
                results[request.key] = _DagReplayResult(error=str(exc), error_kind="compile")

        evidence = _parse_evidence(gate_context.get("evidence"))
        context = json.dumps(
            [item.model_dump(mode="json") for item in evidence],
            ensure_ascii=False,
        )
        seen: dict[str, set[str]] = {key: set() for key in instances}
        try:
            for _ in range(
                min(
                    _MAX_REPLAY_ROUNDS,
                    max((len(instance.dag.nodes) for instance in instances.values()), default=0)
                    + 1,
                )
            ):
                active: list[DagInstance] = []
                for key, instance in instances.items():
                    signature = _instance_signature(instance)
                    if instance.state == "completed" or signature in seen[key]:
                        continue
                    seen[key].add(signature)
                    active.append(instance)
                if not active:
                    break

                values = await self.dag_decider.decide(
                    active,
                    evidence=evidence,
                    context=context,
                )
                changed = False
                for instance in active:
                    slot_values = values.get(instance.experience_uri, {})
                    changed_values = {
                        name: value
                        for name, value in slot_values.items()
                        if instance.slot_values.get(name) != value
                    }
                    if not changed_values:
                        continue
                    instance.merge_slot_values(changed_values)
                    (
                        actions_by_key[instance.experience_uri],
                        waiting_by_key[instance.experience_uri],
                    ) = instance.advance()
                    changed = True
                if not changed:
                    break
        except Exception as exc:
            error = str(exc)
            for key in instances:
                results[key] = _DagReplayResult(error=error, error_kind="replay")
            return results

        for key, instance in instances.items():
            actions = actions_by_key[key]
            results[key] = _DagReplayResult(
                replay=_replay_summary(
                    instance,
                    actions=actions,
                    waiting=waiting_by_key[key],
                ),
                actions=actions,
            )
        return results

    async def _judge_corrective_actions_batch(
        self,
        validations: list[tuple[_CandidateValidation, _DagReplayResult]],
        *,
        replay_results: dict[str, _DagReplayResult],
    ) -> dict[int, float]:
        candidates: dict[str, Any] = {}
        questions: dict[str, dict[str, Any]] = {}
        question_to_ordinal: dict[str, int] = {}
        for index, (validation, candidate) in enumerate(validations):
            question_id = (
                "improvement_effective" if index == 0 else f"improvement_effective_{index}"
            )
            baseline = replay_results.get(_replay_key(validation, "baseline"))
            gate_context = validation.gate_context
            candidates[question_id] = {
                "trajectory": _clip(
                    str(gate_context.get("trajectory_summary") or ""),
                    6000,
                ),
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
                "candidate_source": _clip(validation.source, 8000),
                "candidate_replay": candidate.replay,
                "current_actions": [action.model_dump(mode="json") for action in candidate.actions],
                "baseline_source": (
                    _clip(validation.baseline_source, 8000)
                    if validation.baseline_source is not None
                    else None
                ),
                "baseline_replay": baseline.replay if baseline is not None else None,
                "baseline_error": baseline.error if baseline is not None else None,
            }
            comparison = (
                "materially improves on the baseline Experience and "
                if validation.baseline_source is not None
                else ""
            )
            questions[question_id] = {
                "type": "noul",
                "instructions": (
                    f"For candidate {question_id}, does its current actionable instruction "
                    f"{comparison}directly address the recorded evaluation failure, so following "
                    "it would prevent the same failure? Be strict: an unchanged, generic, "
                    "unrelated, or already-satisfied instruction is false."
                ),
                "criteria": {
                    "true": (
                        "The candidate is better than its baseline when present and directly "
                        "addresses the concrete failed requirement."
                    ),
                    "false": (
                        "The candidate is unchanged, no better than its baseline, generic, "
                        "unrelated, or would allow the same failure."
                    ),
                },
            }
            question_to_ordinal[question_id] = validation.ordinal

        answers = await self.jev.evaluate(
            state={"candidates": candidates},
            questions=questions,
        )
        scores: dict[int, float] = {}
        for question_id, ordinal in question_to_ordinal.items():
            answer = answers.get(question_id)
            score = answer.get("noul") if isinstance(answer, dict) else None
            scores[ordinal] = (
                float(score)
                if isinstance(score, (int, float)) and not isinstance(score, bool)
                else 0.0
            )
        return scores


def _gate_context_key(gate_context: dict[str, Any]) -> str:
    """Group DAGs that can be decided from the same normalized evidence."""
    evidence = _parse_evidence(gate_context.get("evidence"))
    return json.dumps(
        [item.model_dump(mode="json") for item in evidence],
        ensure_ascii=False,
        sort_keys=True,
    )


def _replay_key(validation: _CandidateValidation, role: str) -> str:
    return f"{validation.experience_uri}#improvement-gate-{role}-{validation.ordinal}"


def _baseline_diagnostic(
    validation: _CandidateValidation,
    baseline: _DagReplayResult | None,
) -> dict[str, Any]:
    if validation.baseline_source is None:
        return {}
    if baseline is None:
        return {"baseline_replay": None, "baseline_replay_error": "baseline was not replayed"}
    diagnostic = {"baseline_replay": baseline.replay}
    if baseline.error:
        diagnostic["baseline_replay_error"] = baseline.error
    return diagnostic


def _preliminary_validation_result(
    validation: _CandidateValidation,
    *,
    candidate: _DagReplayResult,
    baseline: _DagReplayResult | None,
) -> dict[str, Any] | None:
    gate_context = validation.gate_context
    trajectory_uri = str(gate_context.get("trajectory_uri") or "")
    baseline_diagnostic = _baseline_diagnostic(validation, baseline)
    if candidate.error:
        reason = (
            f"candidate DAG does not compile: {candidate.error}"
            if candidate.error_kind == "compile"
            else f"candidate DAG replay failed: {candidate.error}"
        )
        return {
            "trajectory_uri": trajectory_uri,
            "passed": False,
            "reason": reason,
            **baseline_diagnostic,
        }
    if candidate.replay is None:
        return {
            "trajectory_uri": trajectory_uri,
            "passed": False,
            "reason": "candidate DAG replay did not produce a result",
            **baseline_diagnostic,
        }

    trajectory_passed = bool(gate_context.get("passed"))
    rollout_passed = bool(gate_context.get("rollout_passed", trajectory_passed))
    if trajectory_passed and candidate.replay["state"] == "completed":
        return {
            "trajectory_uri": trajectory_uri,
            "passed": True,
            "reason": "candidate preserves the successful trajectory",
            "replay": candidate.replay,
            **baseline_diagnostic,
        }
    if not trajectory_passed and candidate.replay["state"] == "completed":
        return {
            "trajectory_uri": trajectory_uri,
            "passed": False,
            "reason": "candidate still accepts the failed trajectory as complete",
            "replay": candidate.replay,
            **baseline_diagnostic,
        }
    if not candidate.actions:
        return {
            "trajectory_uri": trajectory_uri,
            "passed": False,
            "reason": "candidate detects an incomplete path but emits no corrective action",
            "replay": candidate.replay,
            **baseline_diagnostic,
        }
    if trajectory_passed and rollout_passed:
        return {
            "trajectory_uri": trajectory_uri,
            "passed": False,
            "reason": "candidate no longer completes for a successful rollout",
            "replay": candidate.replay,
            **baseline_diagnostic,
        }
    return None


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
