from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from openviking.server.auth import get_session_request_context
from openviking.server.identity import RequestContext, Role
from openviking.server.routers import sessions
from openviking.service.experience_runtime import (
    AdvanceExperienceRequest,
    ExperienceRuntime,
    SearchExperienceRequest,
)
from openviking.service.session_service import SessionService
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience_dag_compiler import compile_dag
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking_cli.exceptions import ConflictError, InvalidArgumentError, PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig

EXPERIENCE = "viking://user/u/memories/experiences/order.md"
EXPERIENCE_TWO = "viking://user/u/memories/experiences/order-two.md"
PROGRAM = """dag = workflow("Check an order")
known = ask("Order ID?")
done = call("query_order")
known.then(done)"""


class MemoryFS:
    def __init__(self):
        self.files = {}
        self.find_calls = []
        self.find_memories = [SimpleNamespace(uri=EXPERIENCE)]
        self.lock = asyncio.Lock()
        self._async_agfs = SimpleNamespace(
            pathlock_acquire_exact=self.acquire,
            pathlock_release=self.release,
        )

    def _uri_to_path(self, uri, ctx):
        return f"{ctx.account_id}/{uri}"

    async def acquire(self, path, **kwargs):
        await self.lock.acquire()
        return {"lease_ref": path}

    async def release(self, lease):
        self.lock.release()

    async def read_file(self, uri, ctx):
        key = self._uri_to_path(uri, ctx)
        if key not in self.files:
            raise FileNotFoundError(uri)
        return self.files[key]

    async def write_file(self, uri, content, ctx, **kwargs):
        self.files[self._uri_to_path(uri, ctx)] = content

    async def stat(self, uri, **kwargs):
        return {}

    async def find(self, **kwargs):
        self.find_calls.append(kwargs)
        return SimpleNamespace(memories=list(self.find_memories))


@pytest.fixture
def env():
    ctx = RequestContext(user=UserIdentifier(account_id="acc", user_id="u"), role=Role.USER)
    fs = MemoryFS()
    raw = MemoryFileUtils.write(MemoryFile(content=PROGRAM, memory_type="experiences"))
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = raw
    model = SimpleNamespace(get_completion_async=AsyncMock(return_value='{"slot_values":{}}'))
    resolver = SimpleNamespace(get_vlm=AsyncMock(return_value=model))
    return ctx, fs, model, resolver


def request(context="User asks about an order"):
    return AdvanceExperienceRequest(experience_uri=EXPERIENCE, context=context)


@pytest.mark.asyncio
async def test_resume_pinned_graph_and_repeat_unexecuted_action(env):
    ctx, fs, model, resolver = env
    service = ExperienceRuntime(fs, resolver)
    first = await service.advance("viking://session/s1", request(), ctx)
    assert first.actions[0].provider.question == "Order ID?"
    assert first.revision == 1
    # Another worker/restart uses the persisted state; updated template is not
    # applied to a running instance.
    replacement = PROGRAM.replace("Order ID?", "New question")
    await fs.write_file(
        EXPERIENCE,
        MemoryFileUtils.write(MemoryFile(content=replacement, memory_type="experiences")),
        ctx,
    )
    second = await ExperienceRuntime(fs, resolver).advance("viking://session/s1", request(), ctx)
    assert second.actions == first.actions
    assert second.revision == 2
    model.get_completion_async.return_value = '{"slot_values":{"known":true}}'
    third = await service.advance("viking://session/s1", request("Order 123"), ctx)
    assert third.actions[0].provider.tool_name == "query_order"
    model.get_completion_async.return_value = '{"slot_values":{"done":true}}'
    fourth = await service.advance(
        "viking://session/s1", request("query_order completed successfully"), ctx
    )
    assert fourth.state == "completed"
    model.get_completion_async.reset_mock()
    assert (await service.advance("viking://session/s1", request(), ctx)).actions == []
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_exp_selects_experience_and_returns_current_instructions(env):
    ctx, fs, model, resolver = env
    result = await ExperienceRuntime(fs, resolver).search(
        "viking://session/s1",
        SearchExperienceRequest(
            context="system text\nOrder 123",
            evidence=[
                {"id": "system:0", "kind": "system_message", "summary": "system text"},
                {"id": "message:1", "kind": "user_message", "summary": "Order 123"},
            ],
        ),
        ctx,
    )

    assert result.query == "Order 123"
    assert result.matched_experience_uris == [EXPERIENCE]
    assert result.active_experience_uris == [EXPERIENCE]
    assert result.instructions[0].experience_uri == EXPERIENCE
    assert result.instructions[0].slot_name == "known"
    assert result.experiences[0].state == "running"
    assert fs.find_calls[0]["target_uri"] == "viking://user/u/memories/experiences"
    assert fs.find_calls[0]["score_threshold"] == 0.3
    assert fs.find_calls[0]["level"] == [2]

    fs.find_memories = []
    model.get_completion_async.return_value = '{"slot_values":{"known":true}}'
    resumed = await ExperienceRuntime(fs, resolver).search(
        "viking://session/s1",
        SearchExperienceRequest(context="The lookup can continue", evidence=[]),
        ctx,
    )
    assert resumed.matched_experience_uris == []
    assert resumed.active_experience_uris == [EXPERIENCE]
    assert resumed.instructions[0].slot_name == "done"


@pytest.mark.asyncio
async def test_search_exp_batches_all_active_dags_through_one_decider_call(env):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE_TWO, ctx)] = fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    fs.find_memories = [SimpleNamespace(uri=EXPERIENCE), SimpleNamespace(uri=EXPERIENCE_TWO)]
    decider = SimpleNamespace(
        decide=AsyncMock(
            return_value={
                EXPERIENCE: {"known": True, "done": True},
                EXPERIENCE_TWO: {"known": True, "done": True},
            }
        )
    )
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="jev"),
        dag_decider=decider,
    )

    result = await runtime.search(
        "viking://session/s1",
        SearchExperienceRequest(
            context="Order 123",
            evidence=[{"id": "message:1", "kind": "user_message", "summary": "Order 123"}],
        ),
        ctx,
    )

    assert {item.experience_uri for item in result.experiences} == {
        EXPERIENCE,
        EXPERIENCE_TWO,
    }
    assert all(item.state == "completed" for item in result.experiences)
    assert result.active_experience_uris == []
    assert result.errors == []
    assert decider.decide.await_count == 1
    assert len(decider.decide.await_args.args[0]) == 2
    resolver.get_vlm.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_exp_batches_all_active_dags_through_one_vlm_call(env):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE_TWO, ctx)] = fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    fs.find_memories = [SimpleNamespace(uri=EXPERIENCE), SimpleNamespace(uri=EXPERIENCE_TWO)]
    model.get_completion_async.return_value = json.dumps(
        {
            "answers": {
                "slot_0": {"type": "noul", "noul": 0.99},
                "slot_1": {"type": "noul", "noul": 0.99},
                "slot_2": {"type": "noul", "noul": 0.99},
                "slot_3": {"type": "noul", "noul": 0.99},
            }
        }
    )
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="vlm"),
    )

    result = await runtime.search(
        "viking://session/s1",
        SearchExperienceRequest(
            context="Order 123",
            evidence=[{"id": "message:1", "kind": "user_message", "summary": "Order 123"}],
        ),
        ctx,
    )

    assert {item.experience_uri for item in result.experiences} == {
        EXPERIENCE,
        EXPERIENCE_TWO,
    }
    assert all(item.state == "completed" for item in result.experiences)
    assert result.active_experience_uris == []
    assert result.errors == []
    resolver.get_vlm.assert_awaited_once_with(ctx.account_id)
    model.get_completion_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_jev_config_keeps_experience_active_without_vlm_fallback(env):
    ctx, fs, model, resolver = env
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="jev"),
    )

    result = await runtime.search(
        "viking://session/s1",
        SearchExperienceRequest(context="Order 123", evidence=[]),
        ctx,
    )

    assert result.active_experience_uris == [EXPERIENCE]
    assert result.experiences[0].revision == 0
    assert result.instructions[0].slot_name == "known"
    assert "configuration is missing" in result.errors[0].error
    resolver.get_vlm.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_jev_failure_keeps_experience_active_without_advancing(env):
    ctx, fs, model, resolver = env
    decider = SimpleNamespace(decide=AsyncMock(side_effect=RuntimeError("temporary outage")))
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="jev"),
        dag_decider=decider,
    )

    result = await runtime.search(
        "viking://session/s1",
        SearchExperienceRequest(context="Order 123", evidence=[]),
        ctx,
    )

    assert result.active_experience_uris == [EXPERIENCE]
    assert result.experiences[0].executed_nodes == []
    assert result.instructions[0].slot_name == "known"
    assert result.errors[0].error == "temporary outage"
    resolver.get_vlm.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_and_account_isolation(env):
    ctx, fs, model, resolver = env
    service = ExperienceRuntime(fs, resolver)
    model.get_completion_async.return_value = '{"slot_values":{"known":true}}'
    await service.advance("viking://session/s1", request(), ctx)
    model.get_completion_async.return_value = '{"slot_values":{}}'
    fresh = await service.advance("viking://session/s2", request(), ctx)
    assert fresh.actions[0].node_id == 1
    other = RequestContext(user=UserIdentifier(account_id="other", user_id="u"), role=Role.USER)
    fs.files[fs._uri_to_path(EXPERIENCE, other)] = fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    assert (await service.advance("viking://session/s1", request(), other)).actions[0].node_id == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "viking://user/other/memories/experiences/x.md",
        "viking://user/u/memories/experiences/../x.md",
        "viking://user/u/memories/experiences/.abstract.md",
        "viking://resources/a.md",
    ],
)
async def test_foreign_or_invalid_experience_is_rejected(env, uri):
    ctx, fs, model, resolver = env
    with pytest.raises(PermissionDeniedError):
        await ExperienceRuntime(fs, resolver).advance(
            "viking://session/s1", request().model_copy(update={"experience_uri": uri}), ctx
        )
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        '{"slot_values":{"wrong":true}}',
        '{"slot_values":{"known":"order-123"}}',
        '{"slot_values":{"known":{"reservation_id":"DF89BM"}}}',
        "not json",
    ],
)
async def test_invalid_llm_result_does_not_overwrite_state(env, response):
    ctx, fs, model, resolver = env
    runtime = ExperienceRuntime(fs, resolver)
    await runtime.advance("viking://session/s1", request(), ctx)
    old = dict(fs.files)
    model.get_completion_async.return_value = response
    with pytest.raises(InvalidArgumentError):
        await runtime.advance("viking://session/s1", request(), ctx)
    assert fs.files == old


@pytest.mark.asyncio
async def test_slot_evidence_and_completed_nodes_are_persisted(env):
    ctx, fs, model, resolver = env
    runtime = ExperienceRuntime(fs, resolver)
    evidence = [{"id": "message:1", "kind": "user_message", "summary": "Order 123"}]
    model.get_completion_async.return_value = json.dumps(
        {"slot_values": {"known": True}, "slot_evidence": {"known": ["message:1"]}}
    )

    result = await runtime.advance(
        "viking://session/s1",
        AdvanceExperienceRequest(experience_uri=EXPERIENCE, context="Order 123", evidence=evidence),
        ctx,
    )

    assert result.slot_evidence == {"known": ["message:1"]}
    assert result.node_slots == {1: "known", 2: "done"}
    assert result.completed_nodes[0].model_dump() == {
        "node_id": 1,
        "slot_name": "known",
        "slot_value": True,
        "evidence_refs": ["message:1"],
    }


@pytest.mark.asyncio
async def test_unknown_slot_evidence_does_not_overwrite_state(env):
    ctx, fs, model, resolver = env
    runtime = ExperienceRuntime(fs, resolver)
    await runtime.advance("viking://session/s1", request(), ctx)
    old = dict(fs.files)
    model.get_completion_async.return_value = (
        '{"slot_values":{"known":true},"slot_evidence":{"known":["invented"]}}'
    )

    with pytest.raises(InvalidArgumentError, match="Unknown evidence"):
        await runtime.advance(
            "viking://session/s1",
            AdvanceExperienceRequest(
                experience_uri=EXPERIENCE,
                context="Order 123",
                evidence=[{"id": "message:1", "kind": "user_message", "summary": "Order 123"}],
            ),
            ctx,
        )
    assert fs.files == old


@pytest.mark.asyncio
async def test_concurrent_advance_detects_stale_snapshot(env):
    ctx, fs, model, resolver = env
    entered = 0
    both_ready = asyncio.Event()

    async def fill(**kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_ready.set()
        await both_ready.wait()
        return '{"slot_values":{"known":true}}'

    model.get_completion_async.side_effect = fill
    runtime = ExperienceRuntime(fs, resolver)
    results = await asyncio.gather(
        *(runtime.advance("viking://session/s1", request(), ctx) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    successful = next(result for result in results if not isinstance(result, Exception))
    assert successful.revision == 1
    assert successful.actions[0].node_id == 2
    assert not fs.lock.locked()


@pytest.mark.asyncio
async def test_runtime_rejects_legacy_text_without_migration(env):
    ctx, fs, model, resolver = env
    await fs.write_file(EXPERIENCE, "## Situation\nAn old memory", ctx)
    with pytest.raises(InvalidArgumentError):
        await ExperienceRuntime(fs, resolver).advance("viking://session/s1", request(), ctx)
    assert len(fs.files) == 1
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_exp_http_endpoint_uses_authenticated_session(env, monkeypatch):
    ctx, fs, model, resolver = env
    service = SessionService(viking_fs=fs)
    service._vlm_resolver = resolver
    service.get = AsyncMock(return_value=SimpleNamespace(_session_uri="viking://session/s1"))
    monkeypatch.setattr(sessions, "get_service", lambda: SimpleNamespace(sessions=service))
    app = FastAPI()
    app.include_router(sessions.router)
    app.dependency_overrides[get_session_request_context] = lambda: ctx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/sessions/s1/experiences/search",
            json={"context": "Order 123", "evidence": []},
        )

    assert response.status_code == 200
    assert response.json()["result"]["instructions"][0]["slot_name"] == "known"
    service.get.assert_awaited_once_with("s1", ctx, auto_create=False)


@pytest.mark.asyncio
async def test_python_extraction_to_storage_to_runtime(env, monkeypatch):
    from openviking.session.memory.agent_experience_context_provider import (
        AgentExperienceContextProvider,
    )
    from openviking.session.memory.extract_loop import ExtractLoop
    from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
    from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
    from openviking.session.memory.memory_updater import MemoryUpdater

    ctx, fs, model, resolver = env
    registry = MemoryTypeRegistry()
    provider = AgentExperienceContextProvider(
        messages=[],
        trajectory_summary="An order was checked",
        trajectory_uri="viking://user/u/memories/trajectories/t.md",
    )
    provider._registry = registry
    provider._ctx = ctx
    provider._viking_fs = fs
    provider.prefetch = AsyncMock(return_value=[])
    isolation = MemoryIsolationHandler(
        ctx, provider.get_extract_context(), allowed_memory_types={"experiences"}
    )
    isolation.prepare_messages()
    provider._isolation_handler = isolation
    configuration = SimpleNamespace(
        memory=SimpleNamespace(link_enabled=False, extraction_output_format="python")
    )
    monkeypatch.setattr(
        "openviking.session.memory.extract_loop.get_openviking_config", lambda: configuration
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: configuration)
    # Exercise both the ordinary SDK program parser and the nested DAG program
    # interpreter, including the existing one-retry extraction repair path.
    bad = 'sdk.create_experiences(experience_name="fresh", content="dag.delete_node(999)", supersedes="")\nsdk.commit()'
    good = f'sdk.create_experiences(experience_name="fresh", content={PROGRAM!r}, supersedes="")\nsdk.commit()'
    model.model = "mock"
    model.get_completion_async.side_effect = [bad, good]
    loop = ExtractLoop(
        vlm=model,
        viking_fs=fs,
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
        max_iterations=1,
    )
    loop._check_unread_existing_files = AsyncMock(return_value={})
    resolved, _ = await loop.run()
    assert model.get_completion_async.await_count == 2
    assert len(resolved.upsert_operations) == 1
    op = resolved.upsert_operations[0]
    assert (
        json.loads(compile_dag(op.memory_fields["content"]))["nodes"]["2"]["slot_provider"][
            "tool_name"
        ]
        == "query_order"
    )
    assert "DAG construction failed" in str(model.get_completion_async.call_args)

    updater = MemoryUpdater(registry=registry)
    updater._viking_fs = fs
    await updater._apply_upsert(op, ctx)
    stored = await fs.read_file(op.uris[0], ctx)
    assert MemoryFileUtils.read(stored).memory_type == "experiences"
    model.get_completion_async.side_effect = None
    model.get_completion_async.return_value = '{"slot_values":{"known":true}}'
    result = await ExperienceRuntime(fs, resolver).advance(
        "viking://session/s1",
        AdvanceExperienceRequest(experience_uri=op.uris[0], context="The order ID is 123"),
        ctx,
    )
    assert result.actions[0].provider.tool_name == "query_order"


@pytest.mark.asyncio
async def test_http_client_search_exp_sends_context_and_evidence():
    from urllib.parse import quote

    from openviking_cli.client._http_compat import AsyncHTTPClient

    client = SimpleNamespace(
        _path_segment=lambda value: quote(value, safe=""),
        _request=AsyncMock(return_value={"result": {"instructions": []}}),
        _handle_response_data=lambda value: value,
    )
    evidence = [{"id": "message:1", "kind": "user_message", "summary": "Order 123"}]

    result = await AsyncHTTPClient.search_exp(
        client,
        "session/a",
        "Order 123",
        evidence=evidence,
        limit=2,
        score_threshold=0.2,
    )

    assert result == {"instructions": []}
    client._request.assert_awaited_once_with(
        "POST",
        "/api/v1/sessions/session%2Fa/experiences/search",
        json={
            "context": "Order 123",
            "evidence": evidence,
            "limit": 2,
            "score_threshold": 0.2,
        },
    )
