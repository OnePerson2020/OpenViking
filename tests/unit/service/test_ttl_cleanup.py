# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Queue outcome and live-policy checks for directory TTL cleanup."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.core.ttl import OBJECT_TYPE_EVENT, OBJECT_TYPE_SESSION
from openviking.pyagfs.exceptions import AGFSNetworkError, AGFSTimeoutError
from openviking.service import ttl_cleanup
from openviking.service.task_tracker import TaskStatus, TaskTracker, set_task_tracker
from openviking.storage.queuefs.process_result import ProcessOutcome
from openviking.storage.ttl_registry import TTLRecord


class _TaskStore:
    def __init__(self):
        self.tasks = {}

    async def create(self, task):
        self.tasks[task.task_id] = task

    async def update(self, task):
        self.tasks[task.task_id] = task

    async def get(self, task_id, *, account_id=None, user_id=None):
        return None

    async def list(self, account_id, *, user_id=None):
        return []

    async def delete(self, task_id, *, account_id, user_id=None):
        self.tasks.pop(task_id, None)


SESSION_URI = "viking://user/u1/sessions/s1"
EVENT_URI = "viking://user/u1/memories/events/2026/09/28"
PAST = "2020-01-01T00:00:00.000Z"
FUTURE = "2999-01-01T00:00:00.000Z"


def _record(
    object_type: str = OBJECT_TYPE_SESSION,
    *,
    object_uri: str = SESSION_URI,
    expires_at: str = PAST,
) -> TTLRecord:
    return TTLRecord(
        object_uri=object_uri,
        object_type=object_type,
        account_id="acct",
        user_id="u1",
        expires_at=expires_at,
    )


def _message(record: TTLRecord, *, task_id: str = "task-1") -> dict:
    return ttl_cleanup._ttl_cleanup_message(record=record, task_id=task_id)


def _owner_meta(expires_at: str = PAST) -> str:
    return json.dumps({"expires_at": expires_at})


def _make_service(
    *,
    record: TTLRecord,
    live_content: str | Exception,
    rm_error: Exception | None = None,
):
    registry = None
    read_file = (
        AsyncMock(side_effect=live_content)
        if isinstance(live_content, Exception)
        else AsyncMock(return_value=live_content)
    )
    agfs = SimpleNamespace(
        pathlock_acquire_batch=AsyncMock(return_value={"lease_ref": "batch"}),
        pathlock_release=AsyncMock(),
    )
    viking_fs = SimpleNamespace(
        _async_agfs=agfs,
        _uri_to_path=lambda uri, ctx=None: f"/local/acct/{uri.removeprefix('viking://')}",
        read_file=read_file,
        rm=AsyncMock(side_effect=rm_error),
        _delete_from_vector_store=AsyncMock(),
        _count_cache={"stale": (1, 0)},
    )

    async def raw_read(path):
        return await viking_fs.read_file(
            "viking://" + path.removeprefix("/local/acct/"), include_expired=True
        )

    agfs.read = raw_read
    agfs.stat = AsyncMock(return_value={"isDir": False})
    viking_fs._handle_agfs_read = lambda raw: raw
    queue = SimpleNamespace(enqueue=AsyncMock())
    queue_manager = SimpleNamespace(
        TTL_CLEANUP="ttl_cleanup",
        SEMANTIC="semantic",
        get_queue=lambda name, **kwargs: queue,
        enqueue=AsyncMock(),
    )
    service = SimpleNamespace(viking_fs=viking_fs, _queue_manager=queue_manager)
    cleanup = ttl_cleanup.TTLCleanupService(service=service)
    return cleanup, viking_fs, registry, queue_manager


@pytest.fixture
def tracker():
    value = TaskTracker(_TaskStore())
    set_task_tracker(value)
    try:
        yield value
    finally:
        set_task_tracker(None)


@pytest.mark.asyncio
async def test_policy_extension_wins_under_object_lock(tracker):
    record = _record()
    cleanup, viking_fs, registry, _ = _make_service(record=record, live_content=_owner_meta(FUTURE))

    result = await cleanup._process(_message(record))

    assert result.outcome is ProcessOutcome.SUCCESS
    viking_fs.rm.assert_not_awaited()
    task = await tracker.get(
        "task-1",
        account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
        user_id=ttl_cleanup.SYSTEM_TASK_USER_ID,
    )
    assert task.result == {"deleted": False, "skipped": "live_or_unmanaged"}


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [AGFSNetworkError, AGFSTimeoutError])
async def test_source_outage_with_not_found_text_retries_without_deleting(tracker, error_type):
    record = _record(OBJECT_TYPE_EVENT, object_uri=EVENT_URI)
    cleanup, fs, registry, _ = _make_service(
        record=record, live_content=error_type("backend endpoint not found")
    )

    result = await cleanup._process(_message(record))

    assert result.outcome is ProcessOutcome.FAILED
    fs.rm.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_cleanup_leaves_metadata_without_deleting(monkeypatch, tracker):
    record = _record()
    cleanup, viking_fs, registry, _ = _make_service(record=record, live_content=_owner_meta())
    monkeypatch.setattr(
        ttl_cleanup,
        "_cleanup_settings",
        lambda: SimpleNamespace(enabled=False, check_interval_seconds=45),
    )

    result = await cleanup._process(_message(record))

    assert result.outcome is ProcessOutcome.SUCCESS
    viking_fs.read_file.assert_not_awaited()
    viking_fs.rm.assert_not_awaited()
    task = await tracker.get(
        "task-1",
        account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
        user_id=ttl_cleanup.SYSTEM_TASK_USER_ID,
    )
    assert task.status is TaskStatus.COMPLETED
    assert task.result == {"deleted": False, "skipped": "paused"}


@pytest.mark.asyncio
async def test_ack_settles_failed_attempt_without_reporting_success(tracker):
    from openviking.service.task_queue_middleware import TaskWorkQueueMiddleware
    from openviking.storage.queuefs.queue_middleware import (
        AckContext,
        EnqueueContext,
        ProcessContext,
    )

    record = _record()
    cleanup, _, registry, _ = _make_service(
        record=record,
        live_content=_owner_meta(),
        rm_error=RuntimeError("backend unavailable"),
    )
    middleware = TaskWorkQueueMiddleware(tracker._work_index)
    enqueue = EnqueueContext("ttl_cleanup", _message(record))

    async def persist(ctx):
        ctx.committed = True
        return "message-1"

    await middleware.enqueue(enqueue, persist)
    delivery = {"id": "message-1", "data": enqueue.payload}

    async def process(ctx):
        return await cleanup._process(ctx.message["data"])

    outcome = await middleware.process(
        ProcessContext("ttl_cleanup", delivery, cancel=AsyncMock()),
        process,
    )
    assert outcome.outcome is ProcessOutcome.FAILED

    async def ack(ctx):
        ctx.committed = True

    await middleware.ack(AckContext("ttl_cleanup", "message-1", delivery), ack)
    task = await tracker.get(
        "task-1",
        account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
        user_id=ttl_cleanup.SYSTEM_TASK_USER_ID,
    )
    assert task.status is TaskStatus.FAILED
    assert task.result is None


async def _cleanup_once(cleanup, record):
    """Exercise the strict cleanup body under its required object lease."""
    ctx, lease = await cleanup._acquire_object_lock(record)
    try:
        return await cleanup._cleanup_record(record, ctx, lease)
    finally:
        await cleanup._service.viking_fs._async_agfs.pathlock_release(lease)
