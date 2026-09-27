"""Bridge selected Tau2 experiences to the server-side DAG runtime."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from typing import Any

from openviking.session.memory.experience_dag import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    clip_evidence_summary,
    tool_evidence_summary,
)

EXPERIENCE_REMINDER_MARKER = "[Experience Reminder]"
_MAX_CONTEXT_CHARS = 128 * 1024


class Tau2DagExperienceRuntime:
    def __init__(self) -> None:
        self.session_id = f"tau2_dag_{uuid.uuid4().hex}"
        self.events: list[dict[str, Any]] = []
        self._pending_actions: dict[str, dict[int, dict[str, Any]]] = {}
        self.client: Any = None
        self._client_lock = asyncio.Lock()
        self.session_created = False
        self._injected_instruction_keys: set[tuple[str, int]] = set()
        self._delivered_assistant_messages: list[str] = []
        self.reminder_messages: list[dict[str, Any]] = []

    def record_delivered_assistant_message(self, content: str) -> None:
        text = str(content or "").strip()
        if text:
            self._delivered_assistant_messages.append(text)

    async def _client(self):
        async with self._client_lock:
            if self.client is None:
                from vikingbot.openviking_mount.ov_server import VikingClient

                self.client = await VikingClient.create()
        return self.client

    async def search_exp(self, messages: list[dict[str, Any]]) -> str | None:
        """Select and inject relevant DAG instructions for the current context."""
        client = await self._client()
        if not self.session_created:
            await client.ensure_session(self.session_id)
            self.session_created = True
        evidence, context = _execution_evidence(
            messages,
            delivered_assistant_messages=self._delivered_assistant_messages,
        )
        try:
            search_result = await client.search_exp(self.session_id, context, evidence=evidence)
        except Exception as exc:
            self.events.append(
                {
                    "state": "search_failed",
                    "error": str(exc),
                    "evidence": evidence,
                    "action_outcomes": [],
                }
            )
            return None

        for result in search_result.get("experiences", []):
            uri = str(result.get("experience_uri") or "")
            transition = dict(result)
            transition["evidence"] = evidence
            transition["action_outcomes"] = _action_outcomes(
                self._pending_actions.get(uri, {}), result
            )
            self.events.append(transition)
            self._pending_actions[uri] = {
                action["node_id"]: action for action in result.get("actions", [])
            }
        for error in search_result.get("errors", []):
            state = "decision_failed" if error.get("retryable") else "invalid"
            self.events.append({**error, "state": state, "evidence": evidence})

        instructions = []
        for instruction in search_result.get("instructions", []):
            if not isinstance(instruction, dict):
                continue
            uri = str(instruction.get("experience_uri") or "")
            try:
                node_id = int(instruction.get("node_id"))
            except (TypeError, ValueError):
                continue
            key = (uri, node_id)
            if key in self._injected_instruction_keys:
                continue
            self._injected_instruction_keys.add(key)
            instructions.append(instruction)
        if not instructions:
            return None
        return (
            EXPERIENCE_REMINDER_MARKER
            + "\n"
            + json.dumps(
                instructions,
                ensure_ascii=False,
                indent=2,
            )
        )

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None


def _execution_evidence(
    messages: list[dict[str, Any]],
    *,
    delivered_assistant_messages: list[str] | None = None,
) -> tuple[list[dict[str, str]], str]:
    evidence: list[dict[str, str]] = []
    context: list[dict[str, Any]] = []
    delivered_counts = Counter(delivered_assistant_messages or [])
    delivered_message_indexes: set[int] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = str(message.get("content") or "").strip()
        if message.get("role") != "assistant" or not content or delivered_counts[content] <= 0:
            continue
        delivered_message_indexes.add(index)
        delivered_counts[content] -= 1
    successful_tool_calls = {
        str(message.get("tool_call_id") or "")
        for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id")
        and not str(message.get("content") or "").lstrip().startswith("Error:")
    }
    for index, message in enumerate(messages):
        content = str(message.get("content", "") or "")
        if EXPERIENCE_REMINDER_MARKER in content:
            continue
        if message.get("name") in {"read_experience", "search_experience", "read_file"}:
            continue
        role = str(message.get("role", "unknown"))
        name = str(message.get("name", "") or "")
        if content:
            evidence_id = f"message:{index}"
            kind = (
                "assistant_message_delivered"
                if index in delivered_message_indexes
                else "tool_result"
                if role == "tool"
                else f"{role}_message"
            )
            summary = (
                tool_evidence_summary(name, content)
                if role == "tool"
                else clip_evidence_summary(kind, content, MAX_EVIDENCE_SUMMARY_CHARS)
            )
            evidence.append({"id": evidence_id, "kind": kind, "summary": summary})
            context.append(
                {
                    "evidence_id": evidence_id,
                    "role": role,
                    "tool_name": name or None,
                    "content": clip_evidence_summary(kind, content, MAX_EVIDENCE_SUMMARY_CHARS),
                }
            )
        for call_index, tool_call in enumerate(message.get("tool_calls") or []):
            delivered_content = _delivered_tool_call_content(tool_call, successful_tool_calls)
            if not delivered_content:
                continue
            evidence_id = f"message:{index}:communicate:{call_index}"
            kind = "assistant_message_delivered"
            summary = clip_evidence_summary(
                kind,
                delivered_content,
                MAX_EVIDENCE_SUMMARY_CHARS,
            )
            evidence.append({"id": evidence_id, "kind": kind, "summary": summary})
            context.append(
                {
                    "evidence_id": evidence_id,
                    "role": "assistant",
                    "tool_name": "communicate_with_user",
                    "content": summary,
                }
            )
    return evidence, _bounded_context_json(context)


def _delivered_tool_call_content(
    tool_call: Any,
    successful_tool_calls: set[str],
) -> str | None:
    if not isinstance(tool_call, dict):
        return None
    call_id = str(tool_call.get("id") or "")
    function = tool_call.get("function")
    if not call_id or call_id not in successful_tool_calls or not isinstance(function, dict):
        return None
    if function.get("name") != "communicate_with_user":
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    if not isinstance(arguments, dict):
        return None
    content = str(arguments.get("content") or "").strip()
    return content or None


def _bounded_context_json(context: list[dict[str, Any]]) -> str:
    selected: list[dict[str, Any]] = []
    for item in reversed(context):
        candidate = [item, *selected]
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) > _MAX_CONTEXT_CHARS:
            if selected:
                break
            clipped = dict(item)
            clipped["content"] = str(clipped.get("content") or "")[-_MAX_CONTEXT_CHARS // 2 :]
            return json.dumps([clipped], ensure_ascii=False)
        selected = candidate
    return json.dumps(selected, ensure_ascii=False)


def _action_outcomes(
    previous_actions: dict[int, dict[str, Any]], result: dict[str, Any]
) -> list[dict[str, Any]]:
    completed_ids = {
        int(node["node_id"])
        for node in result.get("completed_nodes", [])
        if isinstance(node, dict) and node.get("node_id") is not None
    }
    current_ids = {
        int(action["node_id"])
        for action in result.get("actions", [])
        if isinstance(action, dict) and action.get("node_id") is not None
    }
    outcomes: list[dict[str, Any]] = []
    for node_id, action in previous_actions.items():
        status = (
            "completed"
            if node_id in completed_ids
            else "pending"
            if node_id in current_ids
            else "superseded"
        )
        outcomes.append(
            {"node_id": node_id, "slot_name": action.get("slot_name"), "status": status}
        )
    for node_id in sorted(current_ids - set(previous_actions)):
        action = next(action for action in result["actions"] if action["node_id"] == node_id)
        outcomes.append(
            {"node_id": node_id, "slot_name": action.get("slot_name"), "status": "issued"}
        )
    return outcomes
