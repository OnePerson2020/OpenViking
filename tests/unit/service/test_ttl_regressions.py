"""Regression coverage for TTL deletion, recovery, visibility and retry boundaries."""

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.core import ttl
from openviking.server.identity import RequestContext, Role
from openviking.service.session_service import SessionService
from openviking.service.task_tracker import TaskTracker, set_task_tracker
from openviking.session.session import Session
from openviking.storage.content_write import ContentWriteCoordinator
from openviking.storage.queuefs.queue_manager import QueueManager
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import AlreadyExistsError, NotFoundError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.ttl_config import TTLConfig
from tests.server.test_content_batch_write import _VFS
from tests.storage.test_transfer_merge_binding import binding_fs as binding_fs
from tests.unit.service.test_ttl_cleanup import (
    _make_service,
    _message,
    _owner_meta,
    _record,
    _TaskStore,
)
from tests.unit.storage.ttl_test_storage import read_record


def _default_ctx():
    return RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)


class _DummyAgfs:
    def stat(self, path, ctx=None):
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint,filename",
    [
        ("write", "event.md"),
        ("write", ".note.MD"),
        ("batch_write", "event.txt"),
        ("batch_write", ".note.TXT"),
        ("replace", "event.custom"),
        ("append", "event"),
    ],
)
@pytest.mark.parametrize("owner", ["user/default", "user/default/peers/assistant"])
async def test_public_write_entrypoints_share_directory_ttl(
    monkeypatch, entrypoint, owner, filename, binding_fs
):
    class Clock(datetime):
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    config = TTLConfig(**{"global": {"mode": "days", "ttl_days": 1}})
    monkeypatch.setattr(ttl, "datetime", Clock)
    monkeypatch.setattr(ttl, "get_openviking_config", lambda: SimpleNamespace(ttl=config))
    root = f"viking://{owner}/memories/events/2026/09/28"
    uri = root + "/" + filename
    ctx = _default_ctx()
    fs = binding_fs
    source = _VFS(root)
    source._async_agfs.pathlock_acquire_exact = AsyncMock(return_value={"lease_ref": "lock-1"})

    async def publish(uri, content, ctx=None, lease_ref=None):
        await fs.write_file(uri, content, ctx=ctx)
        source.files[uri] = content

    source.write_file = publish
    writer = ContentWriteCoordinator(source)
    monkeypatch.setattr(writer, "_refresh_batch", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "openviking.storage.content_write.MemoryUpdater.refresh_schema_overview",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "openviking.storage.content_write.MemoryUpdater.refresh_file_embedding",
        AsyncMock(return_value=False),
    )
    # File frontmatter remains user content; only the owner metadata sets TTL.
    content = "---\nexpires_at: 2999-01-01T00:00:00Z\n---\nevent body"
    if entrypoint != "batch_write":
        await writer.write(
            uri=uri,
            content=content,
            mode="create" if entrypoint == "write" else entrypoint,
            ctx=ctx,
        )
    else:
        await writer.batch_write(
            root_uri=root,
            operations=[{"uri": uri, "content": content, "mode": "create"}],
            ctx=ctx,
        )

    record = await read_record(fs, ctx.account_id, root)
    assert record is not None
    assert record.object_type == "event"
    assert record.expires_at == "2026-01-02T00:00:00.000Z"
    assert "event body" in await fs.read_file(uri, ctx=ctx)
    # Changing defaults alone cannot override the persisted deadline.
    config.global_default.mode = "disabled"
    Clock.current = datetime(2026, 1, 3, tzinfo=timezone.utc)
    for read in (fs.read_file, fs.read_file_bytes):
        with pytest.raises(NotFoundError):
            await read(uri, ctx=ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", ["2026", "notes.md", "events.txt"])
async def test_event_directory_is_visible_without_parsing_it_as_a_file(monkeypatch, directory):
    fs = VikingFS(agfs=_DummyAgfs())
    monkeypatch.setattr(fs.ttl_registry, "account_may_have_records", AsyncMock(return_value=True))
    monkeypatch.setattr(fs._async_agfs, "stat", AsyncMock(return_value={"isDir": True}))
    monkeypatch.setattr(fs._async_agfs, "read", AsyncMock(side_effect=IsADirectoryError()))
    assert await fs._ttl_uri_visible(
        "viking://user/default/memories/events/" + directory, _default_ctx()
    )
    fs._async_agfs.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_bucket_hides_its_own_abstract(monkeypatch):
    fs = VikingFS(agfs=_DummyAgfs())
    ctx = _default_ctx()
    parent = "viking://user/default/memories/events/2026/09/28"
    event = parent + "/e.md"
    parent_path = fs._uri_to_path(parent, ctx=ctx)
    event_path = fs._uri_to_path(event, ctx=ctx)
    secret = "expired-event-only-secret"
    files = {
        event_path: secret.encode(),
        parent_path + "/.ttl.json": b'{"expires_at":"2000-01-01T00:00:00.000Z"}',
        parent_path + "/.abstract.md": ("Summary: " + secret).encode(),
    }

    async def stat(path, **kwargs):
        if path == parent_path:
            return {"name": "2026", "isDir": True}
        if path in files:
            return {"name": path.rsplit("/", 1)[-1], "isDir": False}
        raise FileNotFoundError(path)

    monkeypatch.setattr(fs._async_agfs, "stat", stat)
    monkeypatch.setattr(fs._async_agfs, "read", AsyncMock(side_effect=lambda path: files[path]))
    monkeypatch.setattr(fs.ttl_registry, "account_may_have_records", AsyncMock(return_value=True))
    with pytest.raises(NotFoundError):
        await fs.read_file(event, ctx=ctx)
    with pytest.raises(NotFoundError):
        await fs.abstract(
            parent, ctx=ctx
        )  # L0 is intentionally retained after the L2 event expires.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expiry,visible",
    [
        ("2000-01-01T00:00:00.000Z", False),
        ("2999-01-01T00:00:00.000Z", True),
    ],
)
async def test_summary_frontmatter_does_not_override_owner_deadline(monkeypatch, expiry, visible):
    from openviking.storage.abstract_overview import render_abstract_overview

    fs = VikingFS(agfs=_DummyAgfs())
    ctx = _default_ctx()
    parent = "viking://user/default/memories/events/2026/09/28"
    path = fs._uri_to_path(parent, ctx=ctx)
    files = {
        path + "/.meta.json": json.dumps({"expires_at": expiry}).encode(),
        path + "/.abstract.md": render_abstract_overview(0, parent, "secret")
        .replace("---\n", "---\nexpires_at: 2999-12-01T00:00:00.000Z\n", 1)
        .encode(),
        path + "/.overview.md": render_abstract_overview(1, parent, "secret")
        .replace("---\n", "---\nexpires_at: 2999-12-01T00:00:00.000Z\n", 1)
        .encode(),
    }

    async def stat(candidate, **kwargs):
        if candidate == path:
            return {"name": "2026", "isDir": True}
        if candidate in files:
            return {"name": candidate.rsplit("/", 1)[-1], "isDir": False}
        raise FileNotFoundError(candidate)

    monkeypatch.setattr(fs._async_agfs, "stat", stat)
    monkeypatch.setattr(
        fs._async_agfs, "read", AsyncMock(side_effect=lambda candidate: files[candidate])
    )
    monkeypatch.setattr(fs.ttl_registry, "account_may_have_records", AsyncMock(return_value=True))
    for read in (fs.abstract, fs.overview):
        if visible:
            assert "secret" in await read(parent, ctx=ctx)
        else:
            with pytest.raises(NotFoundError):
                await read(parent, ctx=ctx)
    for filename in (".abstract.md", ".overview.md"):
        for read in (fs.read_file, fs.read_file_bytes):
            if visible:
                assert await read(parent + "/" + filename, ctx=ctx)
            else:
                with pytest.raises(NotFoundError):
                    await read(parent + "/" + filename, ctx=ctx)


def test_failed_cleanup_does_not_reconsume_without_any_wait():
    record = _record()
    cleanup, vfs, _, queue_manager = _make_service(
        record=record,
        live_content=_owner_meta(),
        rm_error=RuntimeError("vector backend unavailable"),
    )
    pending = [_message(record)]
    tracker = TaskTracker(_TaskStore())
    set_task_tracker(tracker)

    class StopEvent(threading.Event):
        waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            return super().wait(0)

    stop = StopEvent()
    attempts = []

    async def enqueue(name, message):
        pending.append(message)

    queue_manager.enqueue.side_effect = enqueue

    class Queue:
        name = QueueManager.TTL_CLEANUP

        def has_dequeue_handler(self):
            return True

        async def size(self):
            if not pending:
                stop.set()
            return len(pending)

        async def dequeue(self):
            message = pending.pop(0)
            attempts.append(message["task_id"])
            await cleanup._process(message)
            if len(attempts) == 5:
                stop.set()
            return message

    manager = QueueManager.__new__(QueueManager)
    manager._poll_interval = 0.1
    try:
        manager._queue_worker_loop(Queue(), stop, 1)
    finally:
        set_task_tracker(None)
    assert attempts == ["task-1"], (
        "failed work must leave the immediate queue until the next directory scan"
    )
    assert stop.waits


@pytest.mark.asyncio
async def test_create_does_not_report_success_for_invisible_expired_session(monkeypatch):
    fs = VikingFS(agfs=_DummyAgfs())
    ctx = _default_ctx()
    uri = "viking://user/default/sessions/s1"
    path = fs._uri_to_path(uri, ctx=ctx)
    files = {
        path
        + "/.meta.json": b'{"session_id":"s1","expires_at":"2000-01-01T00:00:00.000Z","ttl_generation":"old"}'
    }

    async def stat(candidate, **kwargs):
        if candidate == path:
            return {"name": "s1", "isDir": True}
        if candidate in files:
            return {"name": candidate.rsplit("/", 1)[-1], "isDir": False}
        raise FileNotFoundError(candidate)

    monkeypatch.setattr(fs._async_agfs, "stat", stat)
    monkeypatch.setattr(
        fs._async_agfs, "read", AsyncMock(side_effect=lambda candidate: files[candidate])
    )
    monkeypatch.setattr(fs.ttl_registry, "account_may_have_records", AsyncMock(return_value=True))
    service = SessionService.__new__(SessionService)
    service._record_lifecycle_metric = lambda *args: None
    service._new_session_auto_commit_policy = lambda: None
    service.session = lambda context, sid: Session(
        viking_fs=fs, ctx=context, session_id=sid, session_uri=uri
    )
    with pytest.raises(AlreadyExistsError):
        await service.create(ctx, session_id="s1")
