from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmark.tau2.train.dag_experience_runtime import GUIDANCE_MARKER, Tau2DagExperienceRuntime
from openviking.session.memory.experience_dag import MAX_EVIDENCE_SUMMARY_CHARS

URI = "viking://user/default/memories/experiences/order.md"


@pytest.mark.asyncio
async def test_autorecall_contacts_server_without_agent_activation():
    runtime = Tau2DagExperienceRuntime()
    runtime.client = SimpleNamespace(
        ensure_session=AsyncMock(),
        search_exp=AsyncMock(return_value={"instructions": [], "experiences": [], "errors": []}),
    )

    assert await runtime.search_exp([{"role": "user", "content": "Order"}]) is None
    runtime.client.ensure_session.assert_awaited_once_with(runtime.session_id)
    runtime.client.search_exp.assert_awaited_once()


@pytest.mark.asyncio
async def test_autorecall_uses_live_evidence_and_one_session():
    runtime = Tau2DagExperienceRuntime()
    client = SimpleNamespace(
        ensure_session=AsyncMock(),
        close=AsyncMock(),
        search_exp=AsyncMock(
            return_value={
                "instructions": [
                    {
                        "experience_uri": URI,
                        "node_id": 1,
                        "slot_name": "known",
                        "provider": {"type": "AskUser", "question": "Order?"},
                        "description": "Ask the user: Order?",
                    }
                ],
                "experiences": [
                    {
                        "experience_uri": URI,
                        "state": "running",
                        "actions": [{"node_id": 1, "slot_name": "known"}],
                        "waiting_for_context": [],
                        "completed_nodes": [],
                    }
                ],
                "errors": [],
            }
        ),
    )
    runtime.client = client
    messages = [
        {"role": "user", "content": "Order 123"},
        {"role": "user", "content": GUIDANCE_MARKER + " old action"},
        {"role": "tool", "name": "read_experience", "content": "An old SOP"},
        {"role": "assistant", "content": "Checking", "reasoning_content": "private"},
        {"role": "tool", "name": "query_order", "content": "Order confirmed"},
    ]
    assert (await runtime.search_exp(messages)).startswith(GUIDANCE_MARKER)
    await runtime.search_exp(messages)
    client.ensure_session.assert_awaited_once_with(runtime.session_id)
    context = client.search_exp.call_args.args[1]
    evidence = client.search_exp.call_args.kwargs["evidence"]
    assert "Order confirmed" in context and "Order 123" in context
    assert "old action" not in context and "An old SOP" not in context and "private" not in context
    assert evidence[:2] == [
        {"id": "message:0", "kind": "user_message", "summary": "Order 123"},
        {"id": "message:3", "kind": "assistant_message", "summary": "Checking"},
    ]
    assert evidence[2]["id"] == "message:4"
    assert evidence[2]["kind"] == "tool_result"
    assert json.loads(evidence[2]["summary"]) == {
        "tool_name": "query_order",
        "tool_output": "Order confirmed",
    }
    assert runtime.events[0]["action_outcomes"] == [
        {"node_id": 1, "slot_name": "known", "status": "issued"}
    ]
    assert len(runtime.events) == 2
    await runtime.close()
    client.close.assert_awaited_once()


def test_tool_evidence_summary_is_valid_json_after_bounded_truncation():
    from benchmark.tau2.train.dag_experience_runtime import _execution_evidence

    evidence, _ = _execution_evidence(
        [
            {
                "role": "assistant",
                "content": "I will cancel the reservation now.",
            },
            {
                "role": "tool",
                "name": "cancel_reservation",
                "content": 'result with quotes " and slashes \\' * 10_000,
            },
        ]
    )

    assert evidence[0] == {
        "id": "message:0",
        "kind": "assistant_message",
        "summary": "I will cancel the reservation now.",
    }
    assert 4096 < len(evidence[1]["summary"]) <= MAX_EVIDENCE_SUMMARY_CHARS
    parsed = json.loads(evidence[1]["summary"])
    assert parsed["tool_name"] == "cancel_reservation"
    assert parsed["tool_output"].endswith("...[truncated]")


@pytest.mark.asyncio
async def test_invalid_runtime_state_disables_only_that_experience():
    runtime = Tau2DagExperienceRuntime()
    runtime.client = SimpleNamespace(
        ensure_session=AsyncMock(),
        search_exp=AsyncMock(
            side_effect=RuntimeError(
                "Invalid experience DAG or slot values: Invalid value for slot identify_target"
            )
        ),
    )

    guidance = await runtime.search_exp([{"role": "user", "content": "Find my reservation"}])

    assert guidance is None
    assert runtime.events == [
        {
            "state": "search_failed",
            "error": (
                "Invalid experience DAG or slot values: Invalid value for slot identify_target"
            ),
            "evidence": [
                {"id": "message:0", "kind": "user_message", "summary": "Find my reservation"}
            ],
            "action_outcomes": [],
        }
    ]
    assert await runtime.search_exp([{"role": "user", "content": "Continue"}]) is None


def test_each_rollout_has_a_fresh_server_session():
    assert Tau2DagExperienceRuntime().session_id != Tau2DagExperienceRuntime().session_id
