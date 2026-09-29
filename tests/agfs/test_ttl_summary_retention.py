# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""TTL removes L2 with native AGFS/local vectors while retaining every L0/L1."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.core import ttl
from openviking.service.ttl_cleanup import TTLCleanupService
from openviking.storage.abstract_overview import render_abstract_overview
from openviking.storage.document_ttl import get_document_ttl, update_document_expiry
from openviking.storage.errors import StorageException
from openviking.storage.vector_ids import vector_record_id
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.exceptions import NotFoundError
from tests.storage.test_transfer_merge_binding import binding_fs as binding_fs
from tests.storage.test_transfer_merge_binding import indexed_fs as indexed_fs
from tests.storage.test_transfer_merge_binding import root_ctx
from tests.unit.service.test_ttl_cleanup import _cleanup_once


@pytest.mark.asyncio
async def test_session_retention_edit_and_append_with_native_locks(indexed_fs, monkeypatch):
    from openviking.message import TextPart
    from openviking.session import session as session_module
    from openviking.session.session import Session

    fs, _ = indexed_fs
    ctx = root_ctx()
    uri = "viking://user/default/sessions/native-ttl"
    session = Session(viking_fs=fs, session_id="native-ttl", ctx=ctx)
    await session.ensure_exists()
    initial = await asyncio.wait_for(
        update_document_expiry(fs, uri, ctx=ctx, ttl_relative=7), timeout=5
    )
    changed_at = format_iso8601(parse_iso_datetime(initial["received_at"]) + timedelta(days=1))
    monkeypatch.setattr(session_module, "get_current_timestamp", lambda: changed_at)
    await asyncio.wait_for(
        session.add_message_async("user", [TextPart("Real native append")]), timeout=5
    )
    renewed = await get_document_ttl(fs, uri, ctx=ctx)
    assert renewed["received_at"] == changed_at
    assert renewed["ttl_generation"] == initial["ttl_generation"]
    assert parse_iso_datetime(renewed["expires_at"]) - parse_iso_datetime(changed_at) == timedelta(
        days=7
    )
    # Both operations serialize on the session root and preserve the new duration.
    later = format_iso8601(parse_iso_datetime(changed_at) + timedelta(hours=1))
    monkeypatch.setattr(session_module, "get_current_timestamp", lambda: later)
    producer = await fs._async_agfs.pathlock_acquire_exact(fs._uri_to_path(uri, ctx=ctx))
    edit = asyncio.create_task(update_document_expiry(fs, uri, ctx=ctx, ttl_relative=30))
    try:
        await asyncio.sleep(0.05)
        assert not edit.done()
        append = asyncio.create_task(
            session.add_message_async("user", [TextPart("Concurrent native append")])
        )
    finally:
        await fs._async_agfs.pathlock_release(producer)
    await asyncio.wait_for(asyncio.gather(edit, append), timeout=5)
    final = await get_document_ttl(fs, uri, ctx=ctx)
    assert final["ttl_days"] == 30
    assert final["received_at"] == later
    assert parse_iso_datetime(final["expires_at"]) - parse_iso_datetime(later) == timedelta(days=30)
    assert (await fs.ttl_registry.get(ctx.account_id, uri)).expires_at == final["expires_at"]
    assert len((await fs.read_file(uri + "/messages.jsonl", ctx=ctx)).splitlines()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["event", "session"])
@pytest.mark.parametrize("fail_confirmation", [False, True])
async def test_cleanup_keeps_all_summary_bytes_and_vectors(
    indexed_fs, monkeypatch, kind, fail_confirmation
):
    fs, vectors = indexed_fs
    ctx = root_ctx()
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fs)
    owner = {
        "event": "viking://user/default/memories/events/2026/09/01",
        "resource_file": "viking://user/default/resources/document.txt",
        "session": "viking://user/default/sessions/s1",
    }[kind]
    directory = True
    parent = owner if directory else owner.rsplit("/", 1)[0]
    bodies = (
        [owner + "/body.txt", owner + "/nested/.note", owner + "/nested/.meta.json"]
        if directory
        else [owner]
    )
    summaries = {}
    for folder in [parent, *([owner + "/nested"] if directory else [])]:
        for level, filename in [(0, ".abstract.md"), (1, ".overview.md")]:
            uri = folder + "/" + filename
            summaries[uri] = render_abstract_overview(level, folder, f"Retained L{level}").encode()
            await fs.write_file_bytes(uri, summaries[uri], ctx=ctx)
            await vectors.upsert(
                {
                    "id": vector_record_id(ctx.account_id, folder, level),
                    "uri": folder,
                    "level": level,
                    "abstract": f"Retained L{level}",
                    "vector": [0.1, 0.2, 0.3, 0.4],
                },
                ctx=ctx,
            )
    for uri in bodies:
        await fs.write_file_bytes(uri, b"Expired L2", ctx=ctx)
    sibling = parent.rsplit("/", 1)[0] + "/live.txt" if directory else parent + "/live.txt"
    if kind == "session":
        sibling = owner + "-live/messages.jsonl"
        await fs.write_file(
            owner + "-live/.meta.json", json.dumps({"session_id": "s1-live"}), ctx=ctx
        )
    await fs.write_file_bytes(sibling, b"Live sibling", ctx=ctx)
    for uri in [*bodies, sibling, *([owner + "/orphan.txt"] if directory else [])]:
        await vectors.upsert(
            {
                "id": vector_record_id(ctx.account_id, uri, 2),
                "uri": uri,
                "level": 2,
                "abstract": "L2",
                "vector": [0.1, 0.2, 0.3, 0.4],
            },
            ctx=ctx,
        )
    fields = {"expires_at": "2000-01-01T00:00:00.000Z", "ttl_generation": "old"}
    metadata = ttl.ttl_metadata_uri(kind, owner)
    raw = json.dumps(fields)
    await fs.write_file(metadata, raw, ctx=ctx)
    record = await fs.ttl_registry.get(ctx.account_id, owner)
    for uri in bodies:
        with pytest.raises(NotFoundError):
            await fs.read_file_bytes(uri, ctx=ctx)
    cleanup = TTLCleanupService(
        service=SimpleNamespace(viking_fs=fs, fs=SimpleNamespace(rm=fs.rm)),
        service_loop=asyncio.get_running_loop(),
    )
    original = fs._confirm_fs_scope_cleared
    if fail_confirmation:
        monkeypatch.setattr(
            fs, "_confirm_fs_scope_cleared", AsyncMock(side_effect=OSError("retry confirmation"))
        )
        with pytest.raises(StorageException, match="retry confirmation"):
            await _cleanup_once(cleanup, record)
        assert await fs.ttl_registry.get(ctx.account_id, owner) is not None
        monkeypatch.setattr(fs, "_confirm_fs_scope_cleared", original)
    assert (await _cleanup_once(cleanup, record))["deleted"]
    assert await fs.ttl_registry.get(ctx.account_id, owner) is None
    for uri in bodies:
        with pytest.raises(Exception) as missing:
            await fs._async_agfs.stat(fs._uri_to_path(uri, ctx=ctx), bypass_cache=True)
        from openviking.server.error_mapping import is_storage_not_found

        assert is_storage_not_found(missing.value)
    for uri, content in summaries.items():
        assert await fs.read_file_bytes(uri, ctx=ctx) == content
    assert "Retained L0" in await fs.abstract(parent, ctx=ctx)
    assert "Retained L1" in await fs.overview(parent, ctx=ctx)
    assert await fs.read_file_bytes(sibling, ctx=ctx) == b"Live sibling"
    remaining = await vectors.query(ctx=ctx, limit=100)
    assert {(r["uri"], r["level"]) for r in remaining} == {
        *((uri.rsplit("/", 1)[0], 0 if uri.endswith("/.abstract.md") else 1) for uri in summaries),
        (sibling, 2),
    }
    visible = await vectors.query(ctx=ctx, limit=100, include_expired=False)
    assert {r["id"] for r in visible} == {r["id"] for r in remaining}
    if directory:
        # The fence survives alongside the summaries; delayed L2 work stays stale.
        assert json.loads(await fs.read_file(metadata, ctx=ctx, include_expired=True)) == fields
