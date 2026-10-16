"""Active-task checks reuse one restored owner snapshot, with lifecycle updates live."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_previous_safety import FakeAgfs

from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskTracker


@pytest.mark.asyncio
async def test_owner_loaded_once_and_lifecycle_changes_remain_visible():
    store = PersistentTaskStore(FakeAgfs())
    writer = TaskTracker(store)
    owner = {"account_id": "default", "user_id": "alice"}
    task = await writer.create("session_commit", resource_id="s", **owner)
    tracker = TaskTracker(store)
    store.list = AsyncMock(wraps=store.list)
    assert all(
        await asyncio.gather(
            *[tracker.has_running("session_commit", "s", **owner) for _ in range(12)]
        )
    )
    assert store.list.await_count == 1
    assert await tracker.create_if_no_running("session_commit", "s", **owner) is None
    await tracker.complete(task.task_id, {}, **owner)
    assert not await tracker.has_running("session_commit", "s", **owner)
    new = await tracker.create_if_no_running("session_commit", "s", **owner)
    assert new is not None
    assert await tracker.has_running("session_commit", "s", **owner)
    assert store.list.await_count == 1
    assert not await tracker.has_running("session_commit", "s", account_id="default", user_id="bob")
    assert store.list.await_count == 2


@pytest.mark.asyncio
async def test_capacity_eviction_does_not_hide_running_task():
    store = PersistentTaskStore(FakeAgfs())
    tracker = TaskTracker(store)
    owner = {"account_id": "default", "user_id": "alice"}
    await tracker.create("session_commit", resource_id="old", **owner)
    await tracker.create("session_commit", resource_id="new", **owner)
    assert await tracker.has_running("session_commit", "old", **owner)
    tracker.MAX_TASKS = 1
    await tracker._evict_expired()
    assert await tracker.has_running("session_commit", "old", **owner)


@pytest.mark.asyncio
async def test_failed_restore_retries_and_account_invalidation_reloads():
    store = PersistentTaskStore(FakeAgfs())
    tracker = TaskTracker(store)
    owner = {"account_id": "default", "user_id": "alice"}
    original = store.list
    store.list = AsyncMock(side_effect=OSError("unavailable"))
    with pytest.raises(OSError):
        await tracker.has_running("session_commit", "s", **owner)
    store.list = AsyncMock(wraps=original)
    assert not await tracker.has_running("session_commit", "s", **owner)
    tracker.forget_account_tasks("default")
    await tracker.has_running("session_commit", "s", **owner)
    assert store.list.await_count == 2
