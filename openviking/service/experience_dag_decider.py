# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Translate Experience DAG nodes to batched typed decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openviking.models.vlm.llm import parse_json_from_response
from openviking.session.memory.experience_dag import (
    DEFAULT_BRANCH_CHOICE,
    AskUser,
    CallTool,
    Check,
    ConditionalBranch,
    DagEvidenceRef,
    DagInstance,
    GeneralNode,
    IfElseBranch,
    TellAgent,
    clip_evidence_summary,
)
from openviking.telemetry import tracer
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig


@dataclass(frozen=True, slots=True)
class _QuestionTarget:
    experience_uri: str
    slot_name: str
    question_type: str
    allow_false: bool = False
    choices: tuple[str, ...] = ()


_LLM_DECISION_PROMPT = """Evaluate every supplied Experience DAG question against the shared state.
Return only one JSON object with this shape: {"answers": {"question_id": answer}}.
For a `noul` question, answer with {"type": "noul", "noul": <number from 0 to 1>}.
For a `choice` question, answer with {"type": "choice", "choice": "<one listed choice>",
"confidence": <number from 0 to 1>}.
Follow each question's instructions and criteria independently. Omit a question when the state
does not contain enough evidence. Do not call tools and do not add prose outside the JSON object.
"""


@dataclass(slots=True)
class LlmDagDecisionEvaluator:
    """Evaluate a batch of DAG expressions with the account's configured language model."""

    vlm: Any

    @tracer("experience.dag.llm_evaluate", ignore_args=True, ignore_result=True)
    async def evaluate(
        self,
        *,
        state: Any,
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not questions:
            return {}
        response = await self.vlm.get_completion_async(
            messages=[
                {"role": "system", "content": _LLM_DECISION_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"state": state, "questions": questions},
                        ensure_ascii=False,
                    ),
                },
            ],
            thinking=False,
            max_tokens=8192,
        )
        if getattr(response, "tool_calls", None) or getattr(
            response, "finish_reason", "stop"
        ) not in ("stop", "end_turn"):
            raise ValueError("LLM DAG decision did not return a complete text response")
        parsed = parse_json_from_response(response)
        answers = parsed.get("answers") if isinstance(parsed, dict) else None
        if not isinstance(answers, dict):
            raise ValueError("LLM DAG decision response has no answers object")
        if any(not isinstance(answer, dict) for answer in answers.values()):
            raise ValueError("LLM DAG decision answers must be objects")
        return answers


@dataclass(slots=True)
class ExperienceDagDecider:
    config: DagDeciderConfig
    evaluator: Any

    @tracer("experience.dag.decide", ignore_args=True, ignore_result=True)
    async def decide(
        self,
        instances: list[DagInstance],
        *,
        evidence: list[DagEvidenceRef],
        context: str,
    ) -> dict[str, dict[str, bool | str]]:
        questions, targets = _build_questions(instances)
        if not questions:
            return {}

        answers = await self.evaluator.evaluate(
            state=_build_state(
                instances,
                evidence=evidence,
                context=context,
                max_chars=self.config.max_state_chars,
            ),
            questions=questions,
        )

        values: dict[str, dict[str, bool | str]] = {}
        for question_id, target in targets.items():
            answer = answers.get(question_id)
            if not isinstance(answer, dict) or answer.get("type") != target.question_type:
                continue
            value = _answer_value(answer, target, self.config)
            if value is None:
                continue
            values.setdefault(target.experience_uri, {})[target.slot_name] = value
        return values


def _build_questions(
    instances: list[DagInstance],
) -> tuple[dict[str, dict[str, Any]], dict[str, _QuestionTarget]]:
    questions: dict[str, dict[str, Any]] = {}
    targets: dict[str, _QuestionTarget] = {}
    index = 0
    for instance in instances:
        executed = set(instance.executed_nodes)
        for node_id, node in sorted(instance.dag.nodes.items()):
            if node_id in executed:
                continue
            previous = instance.slot_values.get(node.slot_name)
            if previous is True or isinstance(previous, str):
                continue
            question_id = f"slot_{index}"
            index += 1
            question, target = _question_for_node(instance, node)
            questions[question_id] = question
            targets[question_id] = target
    return questions, targets


def _question_for_node(
    instance: DagInstance,
    node: GeneralNode,
) -> tuple[dict[str, Any], _QuestionTarget]:
    prefix = f"Experience {instance.experience_uri}; workflow: {instance.dag.applicability}. "
    provider = node.slot_provider
    if isinstance(node, ConditionalBranch):
        criteria = {
            label: f'The evidence selects the declared branch "{label}".'
            for label in node.branch_mapping
        }
        if node.default_branch is not None:
            criteria[DEFAULT_BRANCH_CHOICE] = (
                "The evidence explicitly selects an alternative outside the other declared "
                "branch labels. Missing or uncertain evidence is not this choice."
            )
        valid_choices = tuple(criteria)
        criteria["__unknown__"] = "The evidence does not identify a choice yet."
        return (
            {
                "type": "choice",
                "instructions": prefix + _node_question(node),
                "criteria": criteria,
            },
            _QuestionTarget(
                experience_uri=instance.experience_uri,
                slot_name=node.slot_name,
                question_type="choice",
                choices=valid_choices,
            ),
        )

    if isinstance(provider, TellAgent):
        criteria = {
            "true": (
                "An assistant_message_delivered evidence item directly establishes that the "
                "instruction was communicated to the user. Ordinary assistant_message text, "
                "including text emitted alongside another tool call, does not count as delivery."
            ),
            "false": (
                "No assistant_message_delivered evidence item establishes that the instruction "
                "was communicated to the user."
            ),
        }
    else:
        criteria = {
            "true": "The evidence directly establishes completion or truth.",
            "false": "The evidence does not establish completion or truth.",
        }
    return (
        {
            "type": "noul",
            "instructions": prefix + _node_question(node),
            "criteria": criteria,
        },
        _QuestionTarget(
            experience_uri=instance.experience_uri,
            slot_name=node.slot_name,
            question_type="noul",
            allow_false=isinstance(node, IfElseBranch),
        ),
    )


def _node_question(node: GeneralNode) -> str:
    provider = node.slot_provider
    if isinstance(node, IfElseBranch):
        if isinstance(provider, AskUser):
            return f"Is the user's answer affirmative for: {provider.question}"
        if isinstance(provider, Check):
            return f"Does the evidence establish this condition: {provider.check_info}"
        return f"Did this node succeed: {node.action_description()}"
    if isinstance(provider, AskUser):
        return f"Has the user provided the information requested here: {provider.question}"
    if isinstance(provider, CallTool):
        return (
            f"Has tool {provider.tool_name} completed successfully for this step: "
            f"{provider.param_suggest}"
        )
    if isinstance(provider, Check):
        return f"Does the evidence establish this condition: {provider.check_info}"
    if isinstance(provider, TellAgent):
        return f"Has the agent delivered this instruction to the user: {provider.instruction}"
    return f"Has DAG slot {node.slot_name} completed successfully?"


def _answer_value(
    answer: dict[str, Any],
    target: _QuestionTarget,
    config: DagDeciderConfig,
) -> bool | str | None:
    if target.question_type == "noul":
        score = answer.get("noul")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return None
        if score >= config.noul_true_threshold:
            return True
        if target.allow_false and score <= config.noul_false_threshold:
            return False
        return None

    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if (
        not isinstance(choice, str)
        or choice not in target.choices
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or confidence < config.choice_confidence_threshold
    ):
        return None
    return choice


def _build_state(
    instances: list[DagInstance],
    *,
    evidence: list[DagEvidenceRef],
    context: str,
    max_chars: int,
) -> dict[str, Any]:
    relevant = [
        item.model_dump(mode="json")
        for item in evidence
        if item.kind
        in {
            "user_message",
            "assistant_message",
            "assistant_message_delivered",
            "tool_result",
        }
        and item.summary.strip()
        and item.summary.strip() != "Reflect on the results and decide next steps."
    ]
    selected: list[dict[str, Any]] = []
    used = 0
    for item in reversed(relevant):
        size = len(item["summary"])
        if selected and used + size > max_chars:
            break
        if size > max_chars:
            item = {
                **item,
                "summary": clip_evidence_summary(
                    str(item.get("kind") or ""),
                    item["summary"],
                    max_chars,
                ),
            }
            size = len(item["summary"])
        selected.append(item)
        used += size
    selected.reverse()
    state: dict[str, Any] = {
        "evidence": selected,
        "previous_slot_values": {
            instance.experience_uri: instance.slot_values for instance in instances
        },
    }
    if not selected:
        state["context"] = context[-max_chars:]
    return state
