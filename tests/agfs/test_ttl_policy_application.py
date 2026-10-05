# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Root configuration changes applied to actual persisted directory lifetimes."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from openviking.config.binding import manager_over_source
from openviking.config.source import MemoryConfigSource
from openviking.message import TextPart
from openviking.service.ttl_policy import patch_ttl_configuration
from openviking.session.session import Session
from openviking.storage.directory_ttl import read_directory_fields
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.exceptions import FailedPreconditionError
from openviking_cli.utils.config import get_openviking_config, set_openviking_config
from openviking_cli.utils.config.ttl_config import TTLConfig
from tests.storage.test_transfer_merge_binding import binding_fs as binding_fs
from tests.storage.test_transfer_merge_binding import root_ctx


@pytest_asyncio.fixture
async def configured_fs(binding_fs):
    original = get_openviking_config()
    manager = manager_over_source(
        MemoryConfigSource(), base_config=original.model_copy(update={"ttl": TTLConfig()})
    )
    await manager.initialize()
    binding_fs.runtime_config_manager = manager
    try:
        yield binding_fs, manager, root_ctx()
    finally:
        set_openviking_config(original)


async def patch(fs, manager, ctx, value):
    return await patch_ttl_configuration(fs, manager, {"ttl": value}, account_id=ctx.account_id)


@pytest.mark.asyncio
async def test_startup_applies_saved_policy_to_history_and_repairs_missing_index(configured_fs):
    from openviking.service.ttl_policy import apply_startup_ttl

    fs, manager, ctx = configured_fs
    owner = "viking://user/default/peers/peer1/memories/events/2020/01/01"
    await fs.write_file(owner + "/body.md", "historical peer event", ctx=ctx)
    original = await read_directory_fields(fs, owner, ctx=ctx)
    # The process stopped after configuration persistence, before application.
    await manager.patch_account(
        ctx.account_id, {"ttl": {"global": {"mode": "days", "ttl_days": 7}}}
    )
    await apply_startup_ttl(fs)
    fields = await read_directory_fields(fs, owner, ctx=ctx)
    assert fields["received_at"] == original["received_at"]
    assert parse_iso_datetime(fields["expires_at"]) == parse_iso_datetime(
        original["received_at"]
    ) + timedelta(days=7)
    record = await fs.ttl_registry.get(ctx.account_id, owner)
    await fs.ttl_registry.remove_if_current(record)
    await apply_startup_ttl(fs)
    assert await fs.ttl_registry.get(ctx.account_id, owner) == record
    assert await read_directory_fields(fs, owner, ctx=ctx) == fields


@pytest.mark.asyncio
async def test_enable_extend_disable_and_reenable_history_from_original_time(configured_fs):
    fs, manager, ctx = configured_fs
    root = "viking://user/default/memories/events"
    owner = root + "/2020/01/01"
    await fs.write_file(owner + "/a.md", "historical date, received recently", ctx=ctx)
    before = await read_directory_fields(fs, owner, ctx=ctx)
    base = format_iso8601(datetime.now(timezone.utc) - timedelta(days=2))
    await fs.write_file(owner + "/.meta.json", json.dumps({**before, "received_at": base}), ctx=ctx)
    await patch(fs, manager, ctx, {"global": {"mode": "days", "ttl_days": 7}})
    fields = await read_directory_fields(fs, owner, ctx=ctx)
    assert parse_iso_datetime(fields["expires_at"]) == parse_iso_datetime(base) + timedelta(days=7)
    await patch(fs, manager, ctx, {"global": {"ttl_days": 30}})
    fields = await read_directory_fields(fs, owner, ctx=ctx)
    assert parse_iso_datetime(fields["expires_at"]) == parse_iso_datetime(base) + timedelta(days=30)
    assert fields["received_at"] == base
    await patch(fs, manager, ctx, {"directories": {root: {"mode": "disabled"}}})
    fields = await read_directory_fields(fs, owner, ctx=ctx)
    assert not fields.get("expires_at") and fields["received_at"] == base
    assert await fs.ttl_registry.get(ctx.account_id, owner) is None
    await patch(fs, manager, ctx, {"directories": {root: None}})
    assert (await read_directory_fields(fs, owner, ctx=ctx))["ttl_days"] == 30


@pytest.mark.asyncio
async def test_priority_absolute_sessions_and_shortening_never_revive_expired(configured_fs):
    fs, manager, ctx = configured_fs
    session = Session(viking_fs=fs, session_id="s1", ctx=ctx)
    await session.ensure_exists()
    base = format_iso8601(datetime.now(timezone.utc) - timedelta(days=2))
    fields = await read_directory_fields(fs, session.uri, ctx=ctx)
    await fs.write_file(
        session.uri + "/.meta.json", json.dumps({**fields, "received_at": base}), ctx=ctx
    )
    root = session.uri.rsplit("/", 1)[0]
    absolute = int((datetime.now(timezone.utc) + timedelta(days=10)).timestamp())
    await patch(
        fs,
        manager,
        ctx,
        {
            "global": {"mode": "days", "ttl_days": 7},
            "sessions": {"mode": "days", "ttl_days": 3},
            "directories": {root: {"mode": "absolute", "ttl_absolute": absolute}},
        },
    )
    await session.add_message_async("user", [TextPart("does not renew absolute TTL")])
    fields = await read_directory_fields(fs, session.uri, ctx=ctx)
    assert int(parse_iso_datetime(fields["expires_at"]).timestamp()) == absolute
    assert not fields.get("ttl_days")
    await patch(fs, manager, ctx, {"global": {"ttl_days": 60}})
    assert (await read_directory_fields(fs, session.uri, ctx=ctx))["expires_at"] == fields[
        "expires_at"
    ]
    await patch(fs, manager, ctx, {"directories": {root: None}})
    assert (await read_directory_fields(fs, session.uri, ctx=ctx))["ttl_days"] == 3
    fields = await read_directory_fields(fs, session.uri, ctx=ctx)
    await fs.write_file(
        session.uri + "/.meta.json", json.dumps({**fields, "received_at": base}), ctx=ctx
    )
    await patch(fs, manager, ctx, {"sessions": {"ttl_days": 1}})
    expired = await read_directory_fields(fs, session.uri, ctx=ctx)
    assert not await fs.exists(session.uri, ctx=ctx)
    await patch(fs, manager, ctx, {"sessions": {"ttl_days": 90}})
    assert await read_directory_fields(fs, session.uri, ctx=ctx) == expired
    await patch(fs, manager, ctx, {"sessions": {"mode": "disabled"}})
    assert await read_directory_fields(fs, session.uri, ctx=ctx) == expired


@pytest.mark.asyncio
async def test_same_patch_retries_partial_metadata_or_index_failure(configured_fs, monkeypatch):
    fs, manager, ctx = configured_fs
    owner = "viking://user/default/memories/events/2026/10/01"
    await fs.write_file(owner + "/a.md", "body", ctx=ctx)
    policy = {"user_events": {"mode": "days", "ttl_days": 7}}
    with monkeypatch.context() as m:
        m.setattr(fs.ttl_registry, "upsert", AsyncMock(side_effect=OSError("index unavailable")))
        with pytest.raises(FailedPreconditionError, match="Retry the same configuration"):
            await patch(fs, manager, ctx, policy)
    await patch(fs, manager, ctx, policy)
    fields = await read_directory_fields(fs, owner, ctx=ctx)
    assert (await fs.ttl_registry.get(ctx.account_id, owner)).expires_at == fields["expires_at"]


@pytest.mark.asyncio
async def test_history_without_reliable_time_reports_incomplete_and_empty_directory_is_skipped(
    configured_fs,
):
    fs, manager, ctx = configured_fs
    root = "viking://user/default/memories/events"
    owner = root + "/2000/01/01"
    await fs._async_agfs.ensure_parent_dirs(fs._uri_to_path(owner + "/old.md", ctx=ctx))
    await fs._async_agfs.write(fs._uri_to_path(owner + "/old.md", ctx=ctx), b"legacy")
    await fs.mkdir(root + "/2000/01/02", ctx=ctx)
    with pytest.raises(FailedPreconditionError) as error:
        await patch(fs, manager, ctx, {"user_events": {"mode": "days", "ttl_days": 7}})
    assert error.value.details["failed_count"] == 1
    assert not (await read_directory_fields(fs, owner, ctx=ctx)).get("expires_at")


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [None, 7])
async def test_sibling_body_io_is_parallel_with_ttl_off_or_on(configured_fs, monkeypatch, days):
    fs, manager, ctx = configured_fs
    if days:
        await patch(fs, manager, ctx, {"global": {"mode": "days", "ttl_days": days}})
    owner = "viking://user/default/memories/events/2026/10/01"
    await fs.write_file(owner + "/seed.md", "first", ctx=ctx)
    original = fs._async_agfs.write
    all_entered, finish = asyncio.Event(), asyncio.Event()
    entered = 0

    async def write(path, data, **kwargs):
        nonlocal entered
        if path.endswith(".parallel"):
            entered += 1
            if entered == 8:
                all_entered.set()
            await finish.wait()
        return await original(path, data, **kwargs)

    monkeypatch.setattr(fs._async_agfs, "write", write)
    tasks = [
        asyncio.create_task(fs.write_file(f"{owner}/{i}.parallel", "body", ctx=ctx))
        for i in range(8)
    ]
    try:
        await asyncio.wait_for(all_entered.wait(), timeout=5)
    finally:
        finish.set()
        await asyncio.gather(*tasks)
    assert entered == 8


@pytest.mark.asyncio
async def test_policy_expiration_waits_for_unmaterialized_body_writer(configured_fs, monkeypatch):
    from openviking.service.ttl_cleanup import TTLCleanupService
    from tests.unit.service.test_ttl_cleanup import _cleanup_once

    fs, manager, ctx = configured_fs
    owner = "viking://user/default/memories/events/2026/10/02"
    await fs.write_file(owner + "/seed.md", "seed", ctx=ctx)
    original = fs._async_agfs.write
    entered, finish = asyncio.Event(), asyncio.Event()

    async def write(path, data, **kwargs):
        if path.endswith("/new.md"):
            entered.set()
            await finish.wait()
        return await original(path, data, **kwargs)

    monkeypatch.setattr(fs._async_agfs, "write", write)
    task = asyncio.create_task(fs.write_file(owner + "/new.md", "in flight", ctx=ctx))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await patch(
            fs, manager, ctx, {"user_events": {"mode": "absolute", "ttl_absolute": 1000000000}}
        )
        record = await fs.ttl_registry.get(ctx.account_id, owner)
        cleanup = TTLCleanupService(service=SimpleNamespace(viking_fs=fs))
        with pytest.raises(Exception, match="lock|Lock|conflict"):
            await _cleanup_once(cleanup, record)
        assert await fs.ttl_registry.get(ctx.account_id, owner) == record
    finally:
        finish.set()
        await task
    assert (await _cleanup_once(cleanup, record))["deleted"]
    assert not await fs.exists(owner, ctx=ctx, include_expired=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_explicit_empty_event_mkdir_is_visible_and_unmanaged(
    configured_fs, monkeypatch, enabled
):
    from openviking.service.fs_service import FSService

    fs, manager, ctx = configured_fs
    if enabled:
        await patch(fs, manager, ctx, {"user_events": {"mode": "days", "ttl_days": 7}})
    monkeypatch.setattr("openviking.service.fs_service.vectorize_directory_meta", AsyncMock())
    service = FSService(viking_fs=fs)
    owner = "viking://user/default/memories/events/2026/10/03"
    await service.mkdir(owner, ctx=ctx)
    assert await fs.exists(owner, ctx=ctx)
    assert not (await read_directory_fields(fs, owner, ctx=ctx)).get("expires_at")
    await fs.write_file(owner + "/first.md", "body", ctx=ctx)
    assert bool((await read_directory_fields(fs, owner, ctx=ctx)).get("expires_at")) == enabled


@pytest.mark.asyncio
async def test_disable_retry_repairs_index_even_after_old_deadline(configured_fs, monkeypatch):
    from openviking.service import ttl_policy

    fs, manager, ctx = configured_fs
    owner = "viking://user/default/memories/events/2026/10/04"
    await fs.write_file(owner + "/body.md", "body", ctx=ctx)
    await patch(fs, manager, ctx, {"user_events": {"mode": "days", "ttl_days": 1}})
    with monkeypatch.context() as m:
        m.setattr(
            fs.ttl_registry,
            "remove_if_current",
            AsyncMock(side_effect=OSError("index unavailable")),
        )
        with pytest.raises(FailedPreconditionError):
            await patch(fs, manager, ctx, {"user_events": {"mode": "disabled"}})
    assert not (await read_directory_fields(fs, owner, ctx=ctx)).get("expires_at")
    assert await fs.ttl_registry.get(ctx.account_id, owner)
    # Any old nonempty deadline is now expired. Persisted cleared metadata is
    # authoritative; an interrupted index removal must still be repairable.
    monkeypatch.setattr(ttl_policy, "hidden_by_ttl", lambda expiry: bool(expiry))
    await patch(fs, manager, ctx, {"user_events": {"mode": "disabled"}})
    assert await fs.ttl_registry.get(ctx.account_id, owner) is None
    assert await fs.read_file(owner + "/body.md", ctx=ctx) == "body"


@pytest.mark.asyncio
async def test_absolute_policy_uses_deadline_without_fabricating_history_time(configured_fs):
    fs, manager, ctx = configured_fs
    owner = "viking://user/default/memories/events/2000/01/01"
    await fs._async_agfs.ensure_parent_dirs(fs._uri_to_path(owner + "/old.md", ctx=ctx))
    await fs._async_agfs.write(fs._uri_to_path(owner + "/old.md", ctx=ctx), b"legacy")
    deadline = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp())
    await patch(fs, manager, ctx, {"user_events": {"mode": "absolute", "ttl_absolute": deadline}})
    fields = await read_directory_fields(fs, owner, ctx=ctx)
    assert int(parse_iso_datetime(fields["expires_at"]).timestamp()) == deadline
    assert not fields.get("received_at") and not fields.get("ttl_days")
    with pytest.raises(FailedPreconditionError):
        await patch(fs, manager, ctx, {"user_events": {"mode": "days", "ttl_days": 7}})
