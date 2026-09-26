from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openviking.models.jev import JevClient, JevError, JevPayloadTooLarge
from openviking.service.experience_dag_decider import ExperienceDagDecider
from openviking.session.memory.experience_dag import (
    Dag,
    DagEvidenceRef,
    DagInstance,
    tool_evidence_summary,
)
from openviking.session.memory.experience_dag_compiler import compile_dag
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig
from openviking_cli.utils.config.jev_config import JevConfig


class FakeResponse:
    def __init__(self, body, *, error: Exception | None = None):
        self.body = body
        self.error = error

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def json(self):
        return self.body


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, *, json):
        self.calls.append((url, json))
        return self.response


class SequenceAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, *, json):
        self.calls.append((url, json))
        return self.responses.pop(0)


def _status_error_response(
    status_code: int,
    *,
    retry_after: str | None = None,
    body: str | None = None,
):
    request = httpx.Request("POST", "https://example.com/v1/systemone")
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    response = httpx.Response(
        status_code,
        request=request,
        headers=headers,
        text=body,
    )
    error = httpx.HTTPStatusError(
        f"status {status_code}",
        request=request,
        response=response,
    )
    return FakeResponse({}, error=error)


def _instance() -> DagInstance:
    source = """dag = workflow("Route a cancellation")
known = ask("Has the user provided the reservation ID?")
eligible = check("Is the reservation eligible?")
cancel = tell("Cancel the reservation")
deny = tell("Explain why cancellation is unavailable")
intent = choose("Which follow-up did the user request?")
modify = tell("Modify the reservation")
known.then(eligible)
eligible.if_true(cancel)
eligible.if_false(deny)
intent.case("cancel", cancel)
intent.case("modify", modify)
intent.default(deny)"""
    return DagInstance(
        experience_uri="viking://user/u/memories/experiences/cancel.md",
        dag=Dag.model_validate_json(compile_dag(source)),
    )


@pytest.mark.asyncio
async def test_generic_jev_client_sends_system_one_payload():
    transport = FakeAsyncClient(
        FakeResponse({"answers": {"is_valid": {"type": "noul", "noul": 0.95}}})
    )
    client = JevClient(
        JevConfig(api_url="https://example.com/v1/systemone", api_key="secret"),
        client=transport,
    )

    answers = await client.evaluate(
        state={"message": "valid"},
        questions={"is_valid": {"type": "noul", "instructions": "Is it valid?"}},
    )

    assert answers["is_valid"]["noul"] == 0.95
    assert transport.calls == [
        (
            "https://example.com/v1/systemone",
            {
                "state": {"message": "valid"},
                "questions": {"is_valid": {"type": "noul", "instructions": "Is it valid?"}},
            },
        )
    ]


@pytest.mark.asyncio
async def test_generic_jev_client_rejects_missing_answers():
    client = JevClient(
        JevConfig(api_url="https://example.com/v1/systemone", api_key="secret"),
        client=FakeAsyncClient(FakeResponse({})),
    )

    with pytest.raises(JevError, match="no answers"):
        await client.evaluate(state="state", questions={"q": {"type": "noul"}})


@pytest.mark.asyncio
async def test_generic_jev_client_retries_529_with_exponential_backoff(monkeypatch):
    sleeps = AsyncMock()
    monkeypatch.setattr("openviking.models.jev.asyncio.sleep", sleeps)
    transport = SequenceAsyncClient(
        [
            _status_error_response(529),
            _status_error_response(529),
            FakeResponse({"answers": {"ok": {"type": "noul", "noul": 0.9}}}),
        ]
    )
    client = JevClient(
        JevConfig(
            api_url="https://example.com/v1/systemone",
            api_key="secret",
            max_retries=3,
            retry_backoff_seconds=0.5,
        ),
        client=transport,
    )

    answers = await client.evaluate(state="state", questions={"ok": {"type": "noul"}})

    assert answers["ok"]["noul"] == 0.9
    assert len(transport.calls) == 3
    assert [call.args[0] for call in sleeps.await_args_list] == [0.5, 1.0]


@pytest.mark.asyncio
async def test_generic_jev_client_respects_retry_after(monkeypatch):
    sleeps = AsyncMock()
    monkeypatch.setattr("openviking.models.jev.asyncio.sleep", sleeps)
    transport = SequenceAsyncClient(
        [
            _status_error_response(529, retry_after="1.75"),
            FakeResponse({"answers": {"ok": {"type": "noul", "noul": 0.9}}}),
        ]
    )
    client = JevClient(
        JevConfig(api_url="https://example.com/v1/systemone", api_key="secret"),
        client=transport,
    )

    await client.evaluate(state="state", questions={"ok": {"type": "noul"}})

    sleeps.assert_awaited_once_with(1.75)


@pytest.mark.asyncio
async def test_generic_jev_client_splits_questions_to_fit_input_budget():
    transport = SequenceAsyncClient(
        [
            FakeResponse({"answers": {"first": {"type": "noul", "noul": 0.9}}}),
            FakeResponse({"answers": {"second": {"type": "noul", "noul": 0.8}}}),
        ]
    )
    client = JevClient(
        JevConfig(
            api_url="https://example.com/v1/systemone",
            api_key="secret",
            max_input_tokens=1024,
        ),
        client=transport,
    )
    questions = {
        "first": {"type": "noul", "instructions": "a" * 2500},
        "second": {"type": "noul", "instructions": "b" * 2500},
    }

    answers = await client.evaluate(state={"message": "state"}, questions=questions)

    assert set(answers) == {"first", "second"}
    assert len(transport.calls) == 2
    assert [set(call[1]["questions"]) for call in transport.calls] == [
        {"first"},
        {"second"},
    ]


@pytest.mark.asyncio
async def test_generic_jev_client_bisects_provider_422_response():
    transport = SequenceAsyncClient(
        [
            _status_error_response(422, body="maximum context length exceeded input_tokens"),
            FakeResponse({"answers": {"first": {"type": "noul", "noul": 0.9}}}),
            FakeResponse({"answers": {"second": {"type": "noul", "noul": 0.8}}}),
        ]
    )
    client = JevClient(
        JevConfig(
            api_url="https://example.com/v1/systemone",
            api_key="secret",
            max_input_tokens=100_000,
        ),
        client=transport,
    )

    answers = await client.evaluate(
        state="state",
        questions={
            "first": {"type": "noul"},
            "second": {"type": "noul"},
        },
    )

    assert set(answers) == {"first", "second"}
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_generic_jev_client_reports_indivisible_oversized_question():
    client = JevClient(
        JevConfig(
            api_url="https://example.com/v1/systemone",
            api_key="secret",
            max_input_tokens=1024,
        ),
        client=FakeAsyncClient(FakeResponse({"answers": {}})),
    )

    with pytest.raises(JevPayloadTooLarge, match="oversized"):
        await client.evaluate(
            state={"message": "x" * 5000},
            questions={"only": {"type": "noul"}},
        )


@pytest.mark.asyncio
async def test_generic_jev_client_does_not_split_unrelated_422_error():
    transport = FakeAsyncClient(
        _status_error_response(422, body="question schema validation failed")
    )
    client = JevClient(
        JevConfig(
            api_url="https://example.com/v1/systemone",
            api_key="secret",
            max_input_tokens=100_000,
        ),
        client=transport,
    )

    with pytest.raises(JevError, match="request failed") as error:
        await client.evaluate(
            state="state",
            questions={
                "first": {"type": "noul"},
                "second": {"type": "noul"},
            },
        )

    assert not isinstance(error.value, JevPayloadTooLarge)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_dag_decider_batches_noul_and_choice_and_compacts_evidence():
    instance = _instance()
    answers = {
        "slot_0": {"type": "noul", "noul": 0.95},
        "slot_1": {"type": "noul", "noul": 0.05},
        "slot_4": {
            "type": "choice",
            "choice": "modify",
            "confidence": 0.9,
            "probabilities": {"cancel": 0.05, "modify": 0.9, "__default__": 0.05},
        },
    }
    jev = SimpleNamespace(evaluate=AsyncMock(return_value=answers))
    decider = ExperienceDagDecider(DagDeciderConfig(provider="jev"), jev=jev)
    evidence = [
        DagEvidenceRef(id="system:0", kind="system_message", summary="large system prompt"),
        DagEvidenceRef(
            id="message:1",
            kind="user_message",
            summary="Please modify the reservation",
        ),
        DagEvidenceRef(
            id="message:2",
            kind="user_message",
            summary="Reflect on the results and decide next steps.",
        ),
    ]

    values = await decider.decide([instance], evidence=evidence, context="duplicated context")

    assert values == {
        instance.experience_uri: {
            "known": True,
            "eligible": False,
            "intent": "modify",
        }
    }
    call = jev.evaluate.await_args.kwargs
    assert call["state"]["evidence"] == [evidence[1].model_dump(mode="json")]
    assert "context" not in call["state"]
    assert len(call["questions"]) == 6
    assert call["questions"]["slot_0"]["type"] == "noul"
    assert call["questions"]["slot_4"]["type"] == "choice"
    assert "__default__" in call["questions"]["slot_4"]["criteria"]
    assert "__unknown__" in call["questions"]["slot_4"]["criteria"]


@pytest.mark.asyncio
async def test_dag_decider_omits_uncertain_or_invalid_answers():
    instance = _instance()
    jev = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "slot_0": {"type": "noul", "noul": 0.5},
                "slot_1": {"type": "noul", "noul": "yes"},
                "slot_4": {
                    "type": "choice",
                    "choice": "invented",
                    "confidence": 1.0,
                },
            }
        )
    )
    decider = ExperienceDagDecider(DagDeciderConfig(provider="jev"), jev=jev)

    assert await decider.decide([instance], evidence=[], context="state") == {}


@pytest.mark.asyncio
async def test_dag_decider_preserves_tool_name_when_clipping_large_evidence():
    instance = _instance()
    jev = SimpleNamespace(evaluate=AsyncMock(return_value={}))
    decider = ExperienceDagDecider(
        DagDeciderConfig(provider="jev", max_state_chars=1024),
        jev=jev,
    )
    evidence = [
        DagEvidenceRef(
            id="tool:1",
            kind="tool_result",
            summary=tool_evidence_summary(
                "cancel_reservation",
                "x" * 10_000,
            ),
        )
    ]

    await decider.decide([instance], evidence=evidence, context="state")

    selected = jev.evaluate.await_args.kwargs["state"]["evidence"][0]
    assert len(selected["summary"]) <= 1024
    payload = json.loads(selected["summary"])
    assert payload["tool_name"] == "cancel_reservation"
    assert payload["tool_output"].endswith("...[truncated]")
