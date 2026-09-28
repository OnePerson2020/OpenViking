# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Independent session retention on real native storage and locks."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.core import ttl
from openviking.service.ttl_cleanup import TTLCleanupService
from openviking.session.ttl_fence import reconcile_session_ttl
from openviking.storage.document_ttl import get_document_ttl, update_document_expiry
from openviking.storage.session_file_ttl import session_file_fields
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.exceptions import NotFoundError
from tests.storage.test_transfer_merge_binding import binding_fs as binding_fs
from tests.storage.test_transfer_merge_binding import indexed_fs as indexed_fs
from tests.storage.test_transfer_merge_binding import root_ctx
from tests.unit.service.test_ttl_cleanup import _cleanup_once


async def seed(fs, ctx):
    root = "viking://user/default/sessions/files"
    base = datetime.now(timezone.utc) - timedelta(days=2)
    await fs.write_file(
        root + "/.meta.json",
        json.dumps(
            {
                "ttl_days": 7,
                "received_at": format_iso8601(base),
                "expires_at": format_iso8601(base + timedelta(days=7)),
                "ttl_generation": "legacy",
                "session_id": "files",
            }
        ),
        ctx=ctx,
    )
    body = root + "/attachments/body.txt"
    await fs.write_file(body, "original", ctx=ctx)
    await fs.write_file(root + "/messages.jsonl", "", ctx=ctx)
    old = await fs.ttl_registry.get(ctx.account_id, root)
    await asyncio.wait_for(update_document_expiry(fs, body, ctx=ctx, ttl_relative=10), 5)
    return root, body, old


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bytes", "metadata", "none"])
async def test_session_content_write_recovery(indexed_fs, monkeypatch, failure):
    fs, _ = indexed_fs
    ctx = root_ctx()
    root, body, old = await seed(fs, ctx)
    initial = await get_document_ttl(fs, body, ctx=ctx)
    original = fs._async_agfs.write
    metadata_path = fs._uri_to_path(ttl.ttl_metadata_uri("session_file", body), ctx=ctx)
    body_path = fs._uri_to_path(body, ctx=ctx)

    async def fail(path, content, *args, **kwargs):
        if failure == "bytes" and path == body_path:
            raise OSError("content failure")
        if failure == "metadata" and path == metadata_path and b"_ttl_pending" not in content:
            raise OSError("metadata failure")
        return await original(path, content, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fs._async_agfs, "write", fail)
        if failure == "none":
            await asyncio.wait_for(fs.write_file_bytes(body, b"updated", ctx=ctx), 5)
        else:
            with pytest.raises(OSError, match="failure"):
                await asyncio.wait_for(fs.write_file_bytes(body, b"updated", ctx=ctx), 5)
    fields = await get_document_ttl(fs, body, ctx=ctx)
    assert fields["ttl_generation"] == initial["ttl_generation"]
    if failure == "bytes":
        assert fields == initial
        assert await fs.read_file(body, ctx=ctx) == "original"
    else:
        assert parse_iso_datetime(fields["expires_at"]) > parse_iso_datetime(initial["expires_at"])
        assert await fs.read_file(body, ctx=ctx) == "updated"
    cleanup = TTLCleanupService(
        service=SimpleNamespace(viking_fs=fs, fs=SimpleNamespace(rm=fs.rm)),
        service_loop=asyncio.get_running_loop(),
    )
    assert (await _cleanup_once(cleanup, old))["skipped"] == "stale_registry_generation"
    lease = await fs._async_agfs.pathlock_acquire_tree(fs._uri_to_path(root, ctx=ctx))
    try:
        meta = await reconcile_session_ttl(
            fs,
            ctx,
            session_uri=root,
            generation="legacy",
            archive_uri=root + "/history/archive_1",
            lease_ref=lease,
        )
        assert meta["ttl_per_file"] and "expires_at" not in meta
    finally:
        await fs._async_agfs.pathlock_release(lease)
    now = parse_iso_datetime(initial["expires_at"]) + timedelta(days=1)
    actual = ttl.is_expired
    monkeypatch.setattr(ttl, "is_expired", lambda value, **_: actual(value, now=now))
    record = await fs.ttl_registry.get(ctx.account_id, body)
    outcome = await _cleanup_once(cleanup, record)
    assert outcome.get("deleted", False) == (failure == "bytes")
    if failure == "bytes":
        with pytest.raises(NotFoundError):
            await fs.write_file(body, "must stay expired", ctx=ctx)
    else:
        assert await fs.read_file(body, ctx=ctx) == "updated"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cp", "mv"])
async def test_session_file_transfer_preserves_retention(indexed_fs, operation):
    fs, _ = indexed_fs
    ctx = root_ctx()
    root, body, _ = await seed(fs, ctx)
    target = root + "/attachments/copied.txt"
    initial = await get_document_ttl(fs, body, ctx=ctx)
    await asyncio.wait_for(getattr(fs, operation)(body, target, ctx=ctx), 5)
    fields = await get_document_ttl(fs, target, ctx=ctx)
    assert {k: v for k, v in fields.items() if k != "uri"} == {
        k: v for k, v in initial.items() if k != "uri"
    }
    assert await fs.read_file(target, ctx=ctx) == "original"
    assert (await fs.ttl_registry.get(ctx.account_id, target)).expires_at == initial["expires_at"]
    assert await fs.exists(body, ctx=ctx) == (operation == "cp")


@pytest.mark.asyncio
async def test_late_session_file_embedding_cannot_recreate_expired_vector(indexed_fs, monkeypatch):
    from openviking.storage.collection_schemas import TextEmbeddingHandler

    fs, _ = indexed_fs
    ctx = root_ctx()
    _, body, _ = await seed(fs, ctx)
    fields = await session_file_fields(fs, body, ctx=ctx)
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fs)
    now = parse_iso_datetime(fields["expires_at"]) + timedelta(days=1)
    actual = ttl.is_expired
    monkeypatch.setattr(ttl, "is_expired", lambda value, **_: actual(value, now=now))
    write = AsyncMock()
    await TextEmbeddingHandler._write_ttl_vector_if_current(
        None, SimpleNamespace(context_data={"uri": body, **fields}), ctx, write
    )
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_ovpack_preserves_independent_deadlines(binding_fs, tmp_path, monkeypatch):
    from openviking.storage.ovpack.operations import export_ovpack, import_ovpack

    fs, ctx = binding_fs, root_ctx()
    root, body, _ = await seed(fs, ctx)
    expected = await session_file_fields(fs, body, ctx=ctx)
    package = await export_ovpack(fs, root, str(tmp_path / "session.ovpack"), ctx)
    await fs.rm(root, recursive=True, ctx=ctx)
    monkeypatch.setattr(
        "openviking.storage.ovpack.operations._enqueue_direct_vectorization", AsyncMock()
    )
    assert await import_ovpack(fs, package, root.rsplit("/", 1)[0], ctx) == root
    assert await session_file_fields(fs, body, ctx=ctx) == expected
    assert await fs.read_file(body, ctx=ctx) == "original"
    assert (await fs.ttl_registry.get(ctx.account_id, body)).expires_at == expected["expires_at"]


@pytest.mark.asyncio
async def test_failed_new_session_file_does_not_leave_expiry(binding_fs, monkeypatch):
    fs, ctx = binding_fs, root_ctx()
    root, _, _ = await seed(fs, ctx)
    fresh = root + "/fresh.txt"
    original = fs._async_agfs.write

    async def fail(path, data, **kwargs):
        if path == fs._uri_to_path(fresh, ctx=ctx):
            raise OSError("new file failed")
        return await original(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fs._async_agfs, "write", fail)
        with pytest.raises(OSError, match="new file failed"):
            await asyncio.wait_for(fs.write_file(fresh, "new file", ctx=ctx), 5)
    assert await fs.ttl_registry.get(ctx.account_id, fresh) is None
    assert not await fs.exists(ttl.ttl_metadata_uri("session_file", fresh), ctx=ctx)
    await fs.write_file(fresh, "retry", ctx=ctx)
    assert (await get_document_ttl(fs, fresh, ctx=ctx))["ttl_days"] == 7


@pytest.mark.asyncio
async def test_session_append_after_child_migration_keeps_sibling_deadline(indexed_fs):
    from openviking.message import TextPart
    from openviking.session.session import Session

    fs, _ = indexed_fs
    ctx = root_ctx()
    root, body, _ = await seed(fs, ctx)
    initial = await get_document_ttl(fs, body, ctx=ctx)
    session = Session(viking_fs=fs, session_id="files", ctx=ctx)
    await asyncio.wait_for(session.add_message_async("user", [TextPart("new message")]), 5)
    assert await get_document_ttl(fs, body, ctx=ctx) == initial
    messages = await get_document_ttl(fs, root + "/messages.jsonl", ctx=ctx)
    assert messages["ttl_days"] == 7
    assert "new message" in await fs.read_file(root + "/messages.jsonl", ctx=ctx)
    assert "expires_at" not in json.loads(await fs.read_file(root + "/.meta.json", ctx=ctx))
