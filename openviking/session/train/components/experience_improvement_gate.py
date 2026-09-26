# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Validate candidate Experience updates by replaying their DAGs with Jev."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from openviking.models.jev import JevClient, JevPayloadTooLarge, estimate_jev_input_tokens
from openviking.service.experience_dag_decider import ExperienceDagDecider
from openviking.session.memory.experience_dag import (
    Dag,
    DagAction,
    DagEvidenceRef,
    DagInstance,
    clip_evidence_summary,
)
from openviking.session.memory.experience_dag_compiler import compile_dag
from openviking.session.train.domain import PolicyPlanItem, PolicySet, PolicyUpdatePlan
from openviking.session.train.interfaces import SemanticGradient
from openviking.telemetry import tracer
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig
from openviking_cli.utils.config.jev_config import JevConfig

EXPERIENCE_GATE_CONTEXTS_KEY = "_experience_gate_contexts"
_MAX_REPLAY_ROUNDS = 32
_MAX_SUCCESS_ANCHORS = 3
_MAX_SUCCESS_WITNESS_CHARS = 20_000
_MAX_SUCCESS_EVIDENCE_ITEM_CHARS = 2_000


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
class _CorrectiveJudgementResult:
    scores: dict[int, float] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    batch_count: int = 0
    split_count: int = 0
    preservation_scores: dict[int, float] = field(default_factory=dict)
    oversized_candidates: list[dict[str, str]] = field(default_factory=list)

    def merge(self, other: "_CorrectiveJudgementResult") -> None:
        self.scores.update(other.scores)
        self.preservation_scores.update(other.preservation_scores)
        self.errors.update(other.errors)
        self.batch_count += other.batch_count
        self.split_count += other.split_count
        self.oversized_candidates.extend(other.oversized_candidates)


@dataclass(slots=True)
class ExperienceImprovementGate:
    """Validate successful proposals or protect successful anchors from failed repairs."""

    config: DagDeciderConfig
    jev_config: JevConfig | None = None
    dag_decider: Any = None
    jev: Any = None

    def __post_init__(self) -> None:
        if self.jev is None and self.jev_config is not None:
            self.jev = JevClient(self.jev_config)
        if self.dag_decider is None and self.jev is not None:
            self.dag_decider = ExperienceDagDecider(config=self.config, jev=self.jev)

    @tracer(
        "train.proposal_gate.experience.validate",
        ignore_args=True,
        ignore_result=True,
    )
    async def validate_proposals(
        self,
        gradients: list[SemanticGradient],
        policy_set: PolicySet,
        context: Any,
    ) -> tuple[list[SemanticGradient], dict[str, Any]]:
        """Validate each Session proposal independently before PatchMerge."""

        accepted: list[SemanticGradient] = []
        candidate_diagnostics: list[dict[str, Any]] = []
        jev_batch_count = 0
        jev_split_count = 0
        oversized_candidates: list[dict[str, str]] = []

        for gradient in gradients:
            proposal_contexts = list(
                _gate_contexts_by_target([gradient]).get(gradient.target_uri or "", {}).values()
            )
            current = next(
                (policy for policy in policy_set.policies if policy.uri == gradient.target_uri),
                None,
            )
            failed_proposal = any(item.get("passed") is False for item in proposal_contexts)
            validation_contexts = proposal_contexts
            gate_basis = "proposal_session"
            if failed_proposal:
                validation_contexts = await _successful_anchor_contexts(current, policy_set)
                gate_basis = "successful_session_regression"
                if not validation_contexts:
                    candidate_diagnostics.append(
                        {
                            "target_uri": gradient.target_uri,
                            "target_name": gradient.target_name,
                            "passed": False,
                            "improved": False,
                            "reason": "failed proposal has no successful Session anchor",
                            "replays": [],
                            "proposal_source_session_uri": _first_source_session_uri(
                                proposal_contexts
                            ),
                        }
                    )
                    continue
            before_content = (
                gradient.before_file.content
                if gradient.before_file is not None
                else current.content
                if current is not None
                else None
            )
            proposal = PolicyPlanItem(
                kind="upsert",
                memory_type="experiences",
                target_name=gradient.target_name,
                target_uri=gradient.target_uri,
                before_content=before_content,
                after_content=gradient.after_file.content,
                base_version=gradient.base_version,
                confidence=gradient.confidence,
                links=list(gradient.links),
            )
            gated = await self._validate(
                PolicyUpdatePlan(items=[proposal], metadata={"gate_stage": "proposal"}),
                [gradient],
                policy_set,
                context,
                contexts_by_target={
                    gradient.target_uri or "": {
                        str(item.get("source_session_uri") or index): item
                        for index, item in enumerate(validation_contexts)
                    }
                },
            )
            gate = dict(gated.metadata.get("experience_improvement_gate") or {})
            for diagnostic in gate.get("candidates") or []:
                candidate_diagnostics.append(
                    {
                        **diagnostic,
                        "gate_basis": gate_basis,
                        "proposal_source_session_uri": _first_source_session_uri(proposal_contexts),
                    }
                )
            jev_batch_count += int(gate.get("jev_batch_count") or 0)
            jev_split_count += int(gate.get("jev_split_count") or 0)
            oversized_candidates.extend(gate.get("oversized_candidates") or [])
            if not gated.items:
                continue

            gradient.metadata["validated_source_sessions"] = _stored_session_contexts(
                proposal_contexts
            )
            gradient.metadata["proposal_gate_anchor_sessions"] = _stored_session_contexts(
                validation_contexts
            )
            gradient.metadata["experience_proposal_gate"] = gate
            accepted.append(gradient)

        accepted_count = len(accepted)
        diagnostics = {
            "experience_improvement_gate": {
                "stage": "proposal",
                "passed": bool(gradients) and accepted_count == len(gradients),
                "atomic": False,
                "accepted_count": accepted_count,
                "rejected_count": len(gradients) - accepted_count,
                "jev_batch_count": jev_batch_count,
                "jev_split_count": jev_split_count,
                "oversized_candidates": oversized_candidates,
                "candidates": candidate_diagnostics,
            }
        }
        return accepted, diagnostics

    @tracer("train.policy_gate.experience.validate", ignore_args=True, ignore_result=True)
    async def validate(
        self,
        plan: PolicyUpdatePlan,
        gradients: list[SemanticGradient],
        policy_set: PolicySet,
        context: Any,
    ) -> PolicyUpdatePlan:
        return await self._validate(plan, gradients, policy_set, context)

    async def _validate(
        self,
        plan: PolicyUpdatePlan,
        gradients: list[SemanticGradient],
        policy_set: PolicySet,
        context: Any,
        contexts_by_target: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> PolicyUpdatePlan:
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

        del policy_set, context
        contexts_by_target = contexts_by_target or _gate_contexts_by_target(gradients)
        validations_by_item: list[list[_CandidateValidation]] = []
        batches: dict[str, list[_CandidateValidation]] = {}
        batch_diagnostics = _CorrectiveJudgementResult()
        ordinal = 0
        for item in upserts:
            validation_contexts = list(contexts_by_target.get(item.target_uri or "", {}).values())

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
                        "reason": "candidate has no source Session validation context",
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
            batch_diagnostics.merge(await self._validate_batch(batch))

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
                    **_aggregate_session_results(item_diagnostics, item_validations),
                    "replays": item_diagnostics,
                }
            )

        for item, validations, diagnostic in zip(
            upserts, validations_by_item, diagnostics, strict=True
        ):
            if diagnostic["passed"]:
                sources = [
                    {
                        k: v
                        for k, v in validation.gate_context.items()
                        if k not in {"evidence", "experience_execution"}
                    }
                    for validation in validations
                ]
                item.metadata.setdefault("patch_metadata", {})["source_sessions"] = sources

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
                "jev_batch_count": batch_diagnostics.batch_count,
                "jev_split_count": batch_diagnostics.split_count,
                "oversized_candidates": batch_diagnostics.oversized_candidates,
                "candidates": diagnostics,
            },
        }
        return PolicyUpdatePlan(items=accepted_items, metadata=metadata)

    async def _validate_batch(
        self,
        validations: list[_CandidateValidation],
    ) -> _CorrectiveJudgementResult:
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
            return _CorrectiveJudgementResult()
        try:
            judgement = await self._judge_corrective_actions_batch(
                needs_judgement,
                replay_results=replay_results,
            )
        except Exception as exc:
            for validation, candidate in needs_judgement:
                validation.result = {
                    "source_session_uri": str(
                        validation.gate_context.get("source_session_uri") or ""
                    ),
                    "passed": False,
                    "reason": f"candidate corrective-action validation failed: {exc}",
                    "replay": candidate.replay,
                    **_baseline_diagnostic(
                        validation,
                        replay_results.get(_replay_key(validation, "baseline")),
                    ),
                }
            return _CorrectiveJudgementResult()

        for validation, candidate in needs_judgement:
            error = judgement.errors.get(validation.ordinal)
            if error:
                validation.result = {
                    "source_session_uri": str(
                        validation.gate_context.get("source_session_uri") or ""
                    ),
                    "passed": False,
                    "reason": f"candidate corrective-action input is oversized: {error}",
                    "replay": candidate.replay,
                    **_baseline_diagnostic(
                        validation,
                        replay_results.get(_replay_key(validation, "baseline")),
                    ),
                }
                continue
            relevance = judgement.scores.get(validation.ordinal, 0.0)
            preservation = judgement.preservation_scores.get(validation.ordinal, 0.0)
            improved = (
                validation.gate_context.get("passed") is False
                and candidate.replay is not None
                and candidate.replay["state"] != "completed"
                and relevance >= self.config.noul_true_threshold
            )
            no_regression = preservation >= self.config.noul_true_threshold
            validation.result = {
                "source_session_uri": str(validation.gate_context.get("source_session_uri") or ""),
                "passed": no_regression,
                "improved": improved and no_regression,
                "reason": "candidate improves the failed Session"
                if improved and no_regression
                else "candidate preserves Session obligations"
                if no_regression
                else "candidate regresses or lacks grounded Session obligations",
                "corrective_action_score": relevance,
                "preservation_score": preservation,
                "replay": candidate.replay,
                **_baseline_diagnostic(
                    validation, replay_results.get(_replay_key(validation, "baseline"))
                ),
            }
        return judgement

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
    ) -> _CorrectiveJudgementResult:
        candidates: dict[str, Any] = {}
        questions: dict[str, dict[str, Any]] = {}
        question_to_ordinal: dict[str, int] = {}
        question_to_validation: dict[str, _CandidateValidation] = {}
        question_groups: dict[str, list[str]] = {}
        for index, (validation, candidate) in enumerate(validations):
            question_id = (
                "improvement_effective" if index == 0 else f"improvement_effective_{index}"
            )
            baseline = replay_results.get(_replay_key(validation, "baseline"))
            gate_context = validation.gate_context
            candidates[question_id] = {
                "session_evidence": _clip(
                    json.dumps(gate_context.get("evidence") or [], ensure_ascii=False),
                    self.config.max_state_chars,
                ),
                "session_outcome": gate_context.get("passed"),
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
            question_to_validation[question_id] = validation
            preservation_id = f"preservation_{index}"
            question_groups[question_id] = [question_id, preservation_id]
            questions[preservation_id] = {
                "type": "noul",
                "instructions": (
                    f"For candidate {question_id}, are its obligations grounded in the raw "
                    "Session evidence and Case, with no regression relative to the baseline? "
                    "Every previously satisfied requirement must remain supported. Do not accept "
                    "weakened checks, deleted requirements, invented tools or unsupported "
                    "business-success claims. An unchanged valid baseline path is non-regressing. "
                    "Without a baseline, require an evidence-grounded actionable workflow. "
                    "An unknown Session outcome must stay unknown; DAG completion is not an "
                    "independent business evaluation."
                ),
            }
            question_to_ordinal[preservation_id] = validation.ordinal
            question_to_validation[preservation_id] = validation

        result = _CorrectiveJudgementResult()
        max_input_tokens = _jev_max_input_tokens(self.jev)
        model = getattr(getattr(self.jev, "config", None), "model", None)

        async def evaluate(candidate_ids: list[str]) -> None:
            question_ids = [qid for cid in candidate_ids for qid in question_groups[cid]]
            batch_questions = {question_id: questions[question_id] for question_id in question_ids}
            batch_candidates = {
                candidate_id: candidates[candidate_id] for candidate_id in candidate_ids
            }
            estimated_tokens = estimate_jev_input_tokens(
                state={"candidates": batch_candidates},
                questions=batch_questions,
                model=model,
            )
            if estimated_tokens > max_input_tokens:
                await split_or_reject(
                    candidate_ids,
                    reason=(
                        f"estimated input {estimated_tokens} exceeds configured Jev budget "
                        f"{max_input_tokens}"
                    ),
                )
                return
            result.batch_count += 1
            try:
                answers = await self.jev.evaluate(
                    state={"candidates": batch_candidates},
                    questions=batch_questions,
                )
            except JevPayloadTooLarge as exc:
                await split_or_reject(candidate_ids, reason=str(exc))
                return
            for question_id in question_ids:
                answer = answers.get(question_id)
                score = answer.get("noul") if isinstance(answer, dict) else None
                scores = (
                    result.preservation_scores
                    if question_id.startswith("preservation_")
                    else result.scores
                )
                scores[question_to_ordinal[question_id]] = (
                    float(score)
                    if isinstance(score, (int, float)) and not isinstance(score, bool)
                    else 0.0
                )

        async def split_or_reject(question_ids: list[str], *, reason: str) -> None:
            if len(question_ids) > 1:
                result.split_count += 1
                midpoint = len(question_ids) // 2
                await evaluate(question_ids[:midpoint])
                await evaluate(question_ids[midpoint:])
                return
            question_id = question_ids[0]
            validation = question_to_validation[question_id]
            result.errors[validation.ordinal] = reason
            result.oversized_candidates.append(
                {
                    "experience_uri": validation.experience_uri,
                    "source_session_uri": str(
                        validation.gate_context.get("source_session_uri") or ""
                    ),
                    "reason": reason,
                }
            )

        await evaluate(list(candidates))
        return result


def _gate_context_key(gate_context: dict[str, Any]) -> str:
    """Group DAGs that can be decided from the same normalized evidence."""
    evidence = _parse_evidence(gate_context.get("evidence"))
    return json.dumps(
        [item.model_dump(mode="json") for item in evidence],
        ensure_ascii=False,
        sort_keys=True,
    )


def _jev_max_input_tokens(jev: Any) -> int:
    value = getattr(getattr(jev, "config", None), "max_input_tokens", None)
    return value if isinstance(value, int) and value > 0 else 28_000


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
    diagnostic = {
        "source_session_uri": str(gate_context.get("source_session_uri") or ""),
        **_baseline_diagnostic(validation, baseline),
    }
    if gate_context.get("error"):
        return {**diagnostic, "passed": False, "reason": gate_context["error"]}
    if not _parse_evidence(gate_context.get("evidence")):
        return {**diagnostic, "passed": False, "reason": "source Session has no replay evidence"}
    if candidate.error or candidate.replay is None:
        error_prefix = (
            "candidate DAG does not compile"
            if candidate.error_kind == "compile"
            else "candidate DAG replay failed"
        )
        return {
            **diagnostic,
            "passed": False,
            "reason": f"{error_prefix}: {candidate.error or 'missing replay'}",
        }
    diagnostic["replay"] = candidate.replay
    if baseline is not None and (baseline.error or baseline.replay is None):
        return {**diagnostic, "passed": False, "reason": "baseline replay failed"}
    outcome = gate_context.get("passed")
    complete = candidate.replay["state"] == "completed"
    if outcome is True and not complete:
        return {
            **diagnostic,
            "passed": False,
            "improved": False,
            "reason": "candidate no longer completes for a successful Session",
        }
    if outcome is False and complete:
        baseline_complete = (
            baseline is not None
            and baseline.replay is not None
            and baseline.replay["state"] == "completed"
        )
        if not baseline_complete:
            return {
                **diagnostic,
                "passed": False,
                "improved": False,
                "reason": "candidate accepts the failed Session as complete",
            }
        # Completion alone cannot establish equivalence: still compare obligations,
        # and never count an accepted failed path as an improvement.
    if not complete and not candidate.actions:
        return {
            **diagnostic,
            "passed": False,
            "reason": "candidate detects an incomplete path but emits no corrective action",
        }
    return None


def _aggregate_session_results(
    results: list[dict[str, Any]], validations: list[_CandidateValidation]
) -> dict[str, Any]:
    preserved = bool(results) and all(result.get("passed") for result in results)
    has_failure = any(v.gate_context.get("passed") is False for v in validations)
    improved = any(result.get("improved") for result in results)
    passed = preserved and (not has_failure or improved)
    return {
        "passed": passed,
        "improved": improved,
        "reason": "Session replay requirements satisfied"
        if passed
        else "a source Session regressed or could not be validated"
        if not preserved
        else "no failed source Session improved",
    }


def _gate_contexts_by_target(
    gradients: list[SemanticGradient],
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for gradient in gradients:
        target = str(gradient.target_uri or "")
        for value in gradient.metadata.get(EXPERIENCE_GATE_CONTEXTS_KEY) or []:
            if isinstance(value, dict) and value.get("source_session_uri"):
                result.setdefault(target, {})[value["source_session_uri"]] = value
    return result


async def _successful_anchor_contexts(
    policy: Any,
    policy_set: PolicySet,
) -> list[dict[str, Any]]:
    if policy is None or policy_set.viking_fs is None:
        return []

    from openviking.session.archive_store import ArchiveStore
    from openviking.session.tool_output_externalizer import ToolOutputExternalizer
    from openviking.session.train.components.gradient_estimator import _messages_to_gate_evidence

    sources = [
        dict(source)
        for source in policy.metadata.get("source_sessions") or []
        if isinstance(source, dict)
        and source.get("passed") is True
        and source.get("source_session_uri")
    ][-_MAX_SUCCESS_ANCHORS:]
    contexts: list[dict[str, Any]] = []
    for source in sources:
        archive_uri = str(source["source_session_uri"])
        session_uri = archive_uri.rsplit("/history/", 1)[0]
        context = dict(source)
        try:
            messages = await ArchiveStore(
                policy_set.viking_fs,
                policy_set.request_context,
                session_uri,
            ).read_messages(archive_uri)
            messages = await ToolOutputExternalizer(
                policy_set.viking_fs,
                session_uri,
                session_uri.rsplit("/", 1)[-1],
                policy_set.request_context,
            ).hydrate_for_extraction(messages, strict=True)
            context["evidence"] = _bounded_success_evidence(_messages_to_gate_evidence(messages))
        except Exception as exc:
            context["error"] = f"successful Session anchor could not be read: {exc}"
        contexts.append(context)
    return contexts


def _bounded_success_evidence(value: Any) -> list[dict[str, str]]:
    items = [
        item.model_dump(mode="json")
        for item in _parse_evidence(value)
        if item.kind != "system_message"
    ]
    clipped = [
        {
            **item,
            "summary": clip_evidence_summary(
                item["kind"],
                item["summary"],
                _MAX_SUCCESS_EVIDENCE_ITEM_CHARS,
            ),
        }
        for item in items
    ]
    if sum(len(item["summary"]) for item in clipped) <= _MAX_SUCCESS_WITNESS_CHARS:
        return clipped

    selected: dict[int, dict[str, str]] = {}
    used = 0
    head_budget = _MAX_SUCCESS_WITNESS_CHARS // 3
    for index, item in enumerate(clipped):
        size = len(item["summary"])
        if used + size > head_budget:
            break
        selected[index] = item
        used += size
    for index in range(len(clipped) - 1, -1, -1):
        item = clipped[index]
        size = len(item["summary"])
        if index in selected or used + size > _MAX_SUCCESS_WITNESS_CHARS:
            continue
        selected[index] = item
        used += size
    return [selected[index] for index in sorted(selected)]


def _stored_session_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value
            for key, value in context.items()
            if key not in {"evidence", "experience_execution", "error"}
        }
        for context in contexts
        if context.get("source_session_uri")
    ]


def _first_source_session_uri(contexts: list[dict[str, Any]]) -> str:
    return next(
        (str(item["source_session_uri"]) for item in contexts if item.get("source_session_uri")),
        "",
    )


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
