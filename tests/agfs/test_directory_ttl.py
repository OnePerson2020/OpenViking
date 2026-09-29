# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Date buckets share one expiry; summaries and other dates survive cleanup."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from openviking.core import ttl
from openviking.service.ttl_cleanup import TTLCleanupService
from openviking.storage.directory_ttl import read_directory_fields
from openviking.storage.document_ttl import update_document_expiry
from openviking_cli.exceptions import ConflictError, InvalidArgumentError, NotFoundError
from openviking_cli.utils.config.ttl_config import TTLConfig
from tests.storage.test_transfer_merge_binding import binding_fs as binding_fs
from tests.storage.test_transfer_merge_binding import root_ctx
from tests.unit.service.test_ttl_cleanup import _cleanup_once


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    config = TTLConfig.model_validate({"global": {"mode": "days", "ttl_days": 7}})
    monkeypatch.setattr(ttl, "get_openviking_config", lambda: SimpleNamespace(ttl=config))


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["viking://user/default", "viking://user/default/peers/p"])
async def test_bucket_owns_expiry_and_cleanup(binding_fs, monkeypatch, prefix):
    fs, ctx = binding_fs, root_ctx()
    root = prefix + "/memories/events/2026/09/28"
    first, second = root + "/first.md", root + "/second.md"
    await fs.write_file(first, "first", ctx=ctx)
    initial = await read_directory_fields(fs, root, ctx=ctx)
    assert initial["ttl_days"] == 7
    assert await fs.ttl_registry.get(ctx.account_id, first) is None
    await fs.write_file(second, "second", ctx=ctx)
    renewed = await read_directory_fields(fs, second, ctx=ctx)
    assert renewed["expires_at"] >= initial["expires_at"]
    record = await fs.ttl_registry.get(ctx.account_id, root)
    assert record.object_type == "event"
    assert record.expires_at == renewed["expires_at"]
    for name in (".abstract.md", ".overview.md"):
        await fs.write_file(root + "/" + name, "summary stays", ctx=ctx)
    assert await read_directory_fields(fs, root, ctx=ctx) == renewed
    sibling = prefix + "/memories/events/2026/09/29/new.md"
    await fs.write_file(sibling, "new", ctx=ctx)
    real_expired = ttl.is_expired
    monkeypatch.setattr(
        ttl, "is_expired", lambda value, **_: value == record.expires_at or real_expired(value)
    )
    for uri in (first, second):
        with pytest.raises(NotFoundError):
            await fs.read_file(uri, ctx=ctx)
    assert await fs.read_file(sibling, ctx=ctx) == "new"
    monkeypatch.setattr(
        "openviking.service.ttl_cleanup.cleanup_not_before", lambda record: record.expires_at
    )
    cleanup = TTLCleanupService(
        service=SimpleNamespace(viking_fs=fs), service_loop=asyncio.get_running_loop()
    )
    assert (await _cleanup_once(cleanup, record))["deleted"]
    assert await fs.exists(root, ctx=ctx)
    for name in (".abstract.md", ".overview.md"):
        assert await fs.read_file(root + "/" + name, ctx=ctx) == "summary stays"
    for uri in (first, second):
        from openviking.pyagfs.exceptions import AGFSNotFoundError

        with pytest.raises(AGFSNotFoundError):
            await fs._async_agfs.stat(fs._uri_to_path(uri, ctx=ctx), bypass_cache=True)


@pytest.mark.asyncio
async def test_failed_write_does_not_renew_and_recovery_uses_durable_bytes(binding_fs, monkeypatch):
    fs, ctx = binding_fs, root_ctx()
    root = "viking://user/default/memories/events/2026/09/28"
    uri = root + "/event.md"
    await fs.write_file(uri, "original", ctx=ctx)
    original = await read_directory_fields(fs, root, ctx=ctx)
    real_write = fs._async_agfs.write

    async def fail(path, data, **kwargs):
        if path == fs._uri_to_path(uri, ctx=ctx):
            raise OSError("failed content")
        return await real_write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fs._async_agfs, "write", fail)
        with pytest.raises(OSError, match="failed content"):
            await fs.write_file(uri, "new", ctx=ctx)
    assert await read_directory_fields(fs, root, ctx=ctx) == original
    assert await fs.read_file(uri, ctx=ctx) == "original"

    async def fail_finalize(path, data, **kwargs):
        if path.endswith("/28/.ttl.json") and b"_ttl_pending" not in data:
            raise OSError("failed finalization")
        return await real_write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fs._async_agfs, "write", fail_finalize)
        with pytest.raises(OSError, match="failed finalization"):
            await fs.write_file(uri, "committed", ctx=ctx)
    recovered = await read_directory_fields(fs, root, ctx=ctx)
    assert recovered["expires_at"] > original["expires_at"]
    assert await fs.read_file(uri, ctx=ctx) == "committed"


@pytest.mark.asyncio
async def test_scope_and_incremental_default(binding_fs, monkeypatch):
    fs, ctx = binding_fs, root_ctx()
    config = TTLConfig()
    monkeypatch.setattr(ttl, "get_openviking_config", lambda: SimpleNamespace(ttl=config))
    root = "viking://user/default/memories/events/2026/09/27"
    await fs.write_file(root + "/old.md", "old", ctx=ctx)
    config.global_default.mode = "days"
    config.global_default.ttl_days = 7
    await fs.write_file(root + "/new.md", "new", ctx=ctx)
    assert not (await read_directory_fields(fs, root, ctx=ctx)).get("expires_at")
    resource = "viking://resources/a.md"
    await fs.write_file(resource, "kept", ctx=ctx)
    assert await fs.ttl_registry.get(ctx.account_id, resource) is None
    for uri in (resource, root + "/old.md"):
        with pytest.raises(InvalidArgumentError):
            await update_document_expiry(fs, uri, ctx=ctx, ttl_relative=7)


@pytest.mark.asyncio
async def test_fixed_bucket_deadline_does_not_renew(binding_fs):
    fs, ctx = binding_fs, root_ctx()
    root = "viking://user/default/memories/events/2026/09/28"
    await fs.write_file(root + "/a.md", "a", ctx=ctx)
    fields = await update_document_expiry(fs, root, ctx=ctx, expires_at="2999-01-01T00:00:00Z")
    await fs.write_file(root + "/b.md", "b", ctx=ctx)
    assert (await read_directory_fields(fs, root, ctx=ctx))["expires_at"] == fields["expires_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cp", "mv"])
async def test_transfer_date_bucket_preserves_its_lifetime(binding_fs, operation):
    fs, ctx = binding_fs, root_ctx()
    source = "viking://user/default/memories/events/2026/09/28"
    target = "viking://user/default/memories/events/2026/09/29"
    await fs.write_file(source + "/a.md", "a", ctx=ctx)
    before = await read_directory_fields(fs, source, ctx=ctx)
    await getattr(fs, operation)(
        source, target, ctx=ctx, **({"recursive": True} if operation == "cp" else {})
    )
    assert await read_directory_fields(fs, target, ctx=ctx) == before
    assert (await fs.ttl_registry.get(ctx.account_id, target)).expires_at == before["expires_at"]
    assert await fs.read_file(target + "/a.md", ctx=ctx) == "a"
    if operation == "mv":
        assert await fs.ttl_registry.get(ctx.account_id, source) is None


@pytest.mark.asyncio
async def test_concurrent_sibling_writes_with_existing_exact_leases(binding_fs):
    fs, ctx = binding_fs, root_ctx()
    root = "viking://user/default/memories/events/2026/09/28"
    await fs.write_file(root + "/seed.md", "seed", ctx=ctx)
    ready = asyncio.Event()
    count = 0

    async def write_child(name):
        nonlocal count
        uri = root + "/" + name
        lease = await fs._async_agfs.pathlock_acquire_exact(fs._uri_to_path(uri, ctx=ctx))
        try:
            count += 1
            if count == 2:
                ready.set()
            await ready.wait()
            await fs.write_file(uri, name, ctx=ctx, lease_ref=lease)
        finally:
            await fs._async_agfs.pathlock_release(lease)

    await asyncio.wait_for(asyncio.gather(write_child("a.md"), write_child("b.md")), 5)
    assert await fs.read_file(root + "/a.md", ctx=ctx) == "a.md"
    assert await fs.read_file(root + "/b.md", ctx=ctx) == "b.md"


@pytest.mark.asyncio
async def test_copy_into_bucket_uses_directory_lifetime(binding_fs):
    fs, ctx = binding_fs, root_ctx()
    root = "viking://user/default/memories/events/2026/09/28"
    source = "viking://temp/incoming.txt"
    await fs.write_file(source, "incoming", ctx=ctx)
    await fs.mkdir(root, ctx=ctx)
    await fs.cp(source, root + "/event.txt", ctx=ctx)
    initial = await read_directory_fields(fs, root, ctx=ctx)
    assert initial["ttl_days"] == 7
    assert await fs.ttl_registry.get(ctx.account_id, root)
    await fs.cp(root + "/event.txt", root + "/copy.txt", ctx=ctx)
    current = await read_directory_fields(fs, root, ctx=ctx)
    assert current["ttl_generation"] == initial["ttl_generation"]
    assert current["expires_at"] >= initial["expires_at"]
    assert await fs.read_file(root + "/copy.txt", ctx=ctx) == "incoming"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["event", "session"])
async def test_delayed_write_rechecks_expiry_after_directory_cleanup(binding_fs, monkeypatch, kind):
    fs, ctx = binding_fs, root_ctx()
    root = (
        "viking://user/default/memories/events/2026/09/28"
        if kind == "event"
        else "viking://user/default/sessions/s1"
    )
    metadata = root + ("/.ttl.json" if kind == "event" else "/.meta.json")
    if kind == "session":
        await fs.write_file(
            metadata,
            json.dumps(
                {"expires_at": "2999-01-01T00:00:00.000Z", "ttl_generation": "session-generation"}
            ),
            ctx=ctx,
        )
    await fs.write_file(root + "/initial.txt", "initial", ctx=ctx)
    fields = await read_directory_fields(fs, root, ctx=ctx)
    lease = await fs._async_agfs.pathlock_acquire_tree(fs._uri_to_path(root, ctx=ctx))
    writer = asyncio.create_task(fs.write_file(root + "/late.txt", "late", ctx=ctx))
    try:
        await asyncio.sleep(0.05)
        assert not writer.done()
        fields.update(expires_at="2000-01-01T00:00:00.000Z", ttl_days=None)
        await fs.write_file(metadata, json.dumps(fields), ctx=ctx, lease_ref=lease)
        record = await fs.ttl_registry.get(ctx.account_id, root)
        cleanup = TTLCleanupService(
            service=SimpleNamespace(viking_fs=fs), service_loop=asyncio.get_running_loop()
        )
        assert (await cleanup._cleanup_record(record, ctx, lease))["deleted"]
    finally:
        await fs._async_agfs.pathlock_release(lease)
    with pytest.raises(NotFoundError):
        await asyncio.wait_for(writer, 5)
    assert not await fs.exists(root + "/late.txt", ctx=ctx, include_expired=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True])
async def test_waiting_expiry_edit_cannot_restore_lifetime_or_change_replacement(
    binding_fs, monkeypatch, recreate
):
    fs, ctx = binding_fs, root_ctx()
    root = "viking://user/default/memories/events/2026/09/28"
    await fs.write_file(root + "/old.md", "original", ctx=ctx)
    waiting, resume = asyncio.Event(), asyncio.Event()
    acquire = fs._async_agfs.pathlock_acquire_tree

    async def delayed_acquire(path, **kwargs):
        if asyncio.current_task() is editing:
            waiting.set()
            await resume.wait()
        return await acquire(path, **kwargs)

    monkeypatch.setattr(fs._async_agfs, "pathlock_acquire_tree", delayed_acquire)
    editing = asyncio.create_task(update_document_expiry(fs, root, ctx=ctx, ttl_relative=30))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    try:
        await fs.rm(root, recursive=True, ctx=ctx)
        if recreate:
            await fs.write_file(root + "/new.md", "replacement", ctx=ctx)
            replacement = await read_directory_fields(fs, root, ctx=ctx)
    finally:
        resume.set()
    with pytest.raises(ConflictError if recreate else (NotFoundError, ConflictError)):
        await asyncio.wait_for(editing, timeout=5)
    if recreate:
        assert await read_directory_fields(fs, root, ctx=ctx) == replacement
    else:
        from openviking.pyagfs.exceptions import AGFSNotFoundError

        # A native tree lease can leave an empty directory for its lock file.
        # Reject the stale edit without restoring TTL metadata or content.
        for uri in (root + "/.ttl.json", root + "/old.md"):
            with pytest.raises(AGFSNotFoundError):
                await fs._async_agfs.stat(fs._uri_to_path(uri, ctx=ctx), bypass_cache=True)
