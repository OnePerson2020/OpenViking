"""Bridge selected Tau2 experiences to the server-side DAG runtime."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

GUIDANCE_MARKER = "[DAG Experience Guidance]"


class Tau2DagExperienceRuntime:
    def __init__(self) -> None:
        self.session_id = f"tau2_dag_{uuid.uuid4().hex}"
        self.events: list[dict[str, Any]] = []
        self._pending_actions: dict[str, dict[int, dict[str, Any]]] = {}
        self.client: Any = None
        self._client_lock = asyncio.Lock()
        self.session_created = False

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
        evidence, context = _execution_evidence(messages)
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

        instructions = search_result.get("instructions", [])
        if not instructions:
            return None
        return GUIDANCE_MARKER + "\n" + json.dumps(instructions, ensure_ascii=False)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None


def _execution_evidence(messages: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str]:
    evidence: list[dict[str, str]] = []
    context: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content = str(message.get("content", "") or "")
        if not content or GUIDANCE_MARKER in content:
            continue
        if message.get("name") in {"read_experience", "search_experience", "read_file"}:
            continue
        evidence_id = f"message:{index}"
        role = str(message.get("role", "unknown"))
        name = str(message.get("name", "") or "")
        kind = "tool_result" if role == "tool" else f"{role}_message"
        evidence.append({"id": evidence_id, "kind": kind, "summary": content[:4096]})
        context.append(
            {
                "evidence_id": evidence_id,
                "role": role,
                "tool_name": name or None,
                "content": content[:4096],
            }
        )
    return evidence, json.dumps(context, ensure_ascii=False)


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
