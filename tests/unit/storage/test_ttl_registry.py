# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Cross-worker visibility of the coarse account TTL marker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.pyagfs.exceptions import (
    AGFSNetworkError,
    AGFSTimeoutError,
)
from openviking.server.identity import RequestContext, Role
from openviking.storage.ttl_registry import (
    TTLRegistry,
)
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import NotFoundError
from openviking_cli.session.user_id import UserIdentifier
from tests.unit.storage.ttl_test_storage import MemoryAGFS as _MemoryAGFS


@pytest.mark.asyncio
@pytest.mark.parametrize("read_kind", ["text", "bytes", "grep"])
async def test_reader_sees_first_ttl_object_imported_by_another_worker(monkeypatch, read_kind):
    class CachedAGFS(_MemoryAGFS):
        async def stat(self, path, *, bypass_cache=False):
            if not bypass_cache:
                raise FileNotFoundError("cached marker miss")
            return await super().stat(path)

    agfs = CachedAGFS()
    reader = TTLRegistry(agfs)
    writer = TTLRegistry(agfs)
    uri = "viking://user/u1/memories/events/2026/09/28/expired.md"
    assert await reader.account_may_have_records("acct") is False
    await writer.mark_account("acct")

    fs = VikingFS(agfs=SimpleNamespace())
    fs.ttl_registry = reader
    ctx = RequestContext(user=UserIdentifier("acct", "u1"), role=Role.ROOT)
    fs._async_agfs.stat = AsyncMock(return_value={"isDir": False})
    fs._async_agfs.read = AsyncMock(return_value=b'{"expires_at":"2000-01-01T00:00:00Z"}')
    if read_kind == "grep":
        fs._async_agfs.grep = AsyncMock(
            return_value={"matches": [{"file": "expired.md", "line": 1, "content": "body"}]}
        )
        result = await fs._grep_with_agfs(uri.rsplit("/", 1)[0], "body", node_limit=1, ctx=ctx)
        assert result["matches"] == []
        assert fs._async_agfs.grep.await_args.kwargs["node_limit"] is None
    else:
        read = fs.read_file if read_kind == "text" else fs.read_file_bytes
        with pytest.raises(NotFoundError):
            await read(uri, ctx=ctx)
    fs._async_agfs.read.assert_awaited()
    assert await reader.account_may_have_records("acct") is True
    # Only positive observations may be reused across requests.
    assert agfs.stat_calls == [reader.marker_path("acct")] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("backend unavailable"),
        AGFSNetworkError("endpoint not found"),
        AGFSTimeoutError("backend not found before timeout"),
    ],
)
async def test_marker_inspection_fails_open_on_storage_error(error):
    class _UnavailableAGFS(_MemoryAGFS):
        async def stat(self, path, **kwargs):
            raise error

    assert await TTLRegistry(_UnavailableAGFS()).account_may_have_records("acct") is True
