"""Local: user-memory prefetch searches every stretch of the batch in every directory,
and the model can search those directories itself."""
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message
from openviking.message.part import TextPart
from openviking.server.identity import RequestContext, Role
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.memory_updater import ExtractContext
from openviking.session.memory.session_extract_context_provider import (
    _PREFETCH_SEARCH_QUERY_MAX_CHARS,
    SessionExtractContextProvider,
)
from openviking_cli.session.user_id import UserIdentifier


def _batch():
    messages = []
    for i in range(40):
        messages.append(Message(id=f"u{i}", role="user", parts=[TextPart(text=f"topic{i} " * 120)]))
        messages.append(Message(id=f"a{i}", role="assistant", parts=[TextPart(text="ok " * 200)]))
    messages.append(Message(id="last", role="user", parts=[TextPart(text="zebra_project late topic")]))
    return messages


def test_late_topics_reach_a_query():
    provider = SessionExtractContextProvider(messages=_batch())
    queries = provider._prefetch_search_queries()

    assert "zebra_project" not in provider._build_prefetch_search_query()  # upstream query
    assert any("zebra_project" in q for q in queries)
    assert all(len(q) <= _PREFETCH_SEARCH_QUERY_MAX_CHARS for q in queries)
    for i in range(40):  # every user message is in some query
        assert any(f"topic{i} " in q for q in queries)


def test_empty_batch_still_has_a_query():
    assert SessionExtractContextProvider(messages=[])._prefetch_search_queries() == ["conversation"]


@pytest.mark.asyncio
async def test_prefetch_searches_each_dir_per_query_and_scopes_model_search():
    messages = _batch()
    ctx = RequestContext(user=UserIdentifier("acc", "u1"), role=Role.USER)
    provider = SessionExtractContextProvider(messages=messages, ctx=ctx)
    provider._isolation_handler = MemoryIsolationHandler(ctx, ExtractContext(messages))
    provider._viking_fs = AsyncMock()
    provider._transaction_handle = None
    provider.read_file = AsyncMock(return_value=None)

    async def search_files(query, search_uris=None, limit=5):
        (d,) = search_uris
        return [f"{d}/shared.md", f"{d}/{abs(hash(query)) % 1000}.md"]

    provider.search_files = AsyncMock(side_effect=search_files)
    out = await provider.prefetch()

    dirs = provider._search_dirs
    n_queries = len(provider._prefetch_search_queries())
    assert {d.rsplit("/", 1)[-1] for d in dirs} >= {"entities", "preferences", "cases"}
    assert provider.search_files.await_count == n_queries * len(dirs)
    shown = [m for m in out if "\"search\"" in m["content"]][0]["content"]
    assert shown.count("entities/shared.md") == 1  # deduplicated
    assert "search" in provider.get_tools()
    assert provider.create_tool_context().default_search_uris == dirs
