"""Uploaded-file response recovery is independent of CAD resource semantics."""

import asyncio
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.parse.understanding_api import UnderstandingAPI
from openviking.server.identity import RequestContext, Role
from openviking.service.resource_service import ResourceService
from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskTracker
from openviking.storage.queuefs.add_resource_msg import AddResourceMsg
from openviking_cli.session.user_id import UserIdentifier
from tests.parse.test_understanding_api_artifact_images import _FakeVikingFS
from tests.test_task_tracker import _FakeAgfs


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["step", "stp", "dwg", "pdf"])
async def test_file_response_is_reused_after_worker_interruption(monkeypatch, tmp_path, extension):
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    task = await tracker.create("add_resource", account_id="acme", user_id="alice")
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.ROOT)
    source_name = "model." + extension
    source = tmp_path / "removed-upload" / source_name
    assert not source.exists()
    archive_path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("model_step/" + source_name, b"original bytes")
        archive.writestr("model_step/evidence.json", '{"dimensions":{"x":1}}')
        for view in ("front", "top", "right", "iso_front_top", "iso_back_top", "iso_front_bottom"):
            archive.writestr("model_step/" + view + ".png", b"view bytes")
    fs = _FakeVikingFS()
    monkeypatch.setattr("openviking.parse.understanding_api.get_viking_fs", lambda: fs)
    api = UnderstandingAPI.__new__(UnderstandingAPI)
    api._video_exts = api._audio_exts = api._image_exts = set()
    api._create_response_for_file = AsyncMock(return_value={"id": "response-1"})
    api._poll_response = AsyncMock(
        side_effect=[
            asyncio.CancelledError(),
            {"result": {"zip_url": "https://example.test/model.zip"}},
        ]
    )
    api._download_zip = AsyncMock(return_value=archive_path)
    calls = []

    async def ingest(**kwargs):
        calls.append(kwargs)
        assert kwargs["watch_interval"] == 60
        assert not any(key.startswith("_step_") for key in kwargs)
        result = await api.parse(kwargs.pop("path"), **kwargs)
        assert result.source_format == ("step" if extension == "stp" else extension)
        assert result.meta["response_id"] == "response-1"
        return {"status": "success", "root_uri": "viking://resources/model"}

    service = ResourceService(viking_fs=fs, resource_processor=object())
    service._execute_resource_ingestion = AsyncMock(side_effect=ingest)
    msg = AddResourceMsg(
        task_id=task.task_id,
        root_uri="viking://resources/model",
        path=str(source),
        source_name=source_name,
        account_id="acme",
        user_id="alice",
        role="root",
        understanding_file_id="file-1",
        watch_interval=60,
    )

    async def execute():
        return await service.execute_add_resource_job(
            msg, ctx=ctx, resource_lock=None, stage_callback=AsyncMock()
        )

    with pytest.raises(asyncio.CancelledError):
        await execute()
    tracker = TaskTracker(store=tracker._store)
    restored = await tracker.get(task.task_id, "acme", "alice")
    assert restored.meta["understanding_response_id"] == "response-1"
    assert await execute() == {"status": "success", "root_uri": "viking://resources/model"}
    api._create_response_for_file.assert_awaited_once_with(file_id="file-1")
    assert api._poll_response.await_count == 2
    assert calls[0]["understanding_file_id"] == "file-1"
    assert "understanding_file_id" not in calls[1]
    assert calls[1]["understanding_response_id"] == "response-1"
    assert len(fs.files) == 8
    assert fs.files["viking://temp/artifact/model/" + source_name] == b"original bytes"
    assert json.loads(fs.files["viking://temp/artifact/model/evidence.json"])["dimensions"] == {
        "x": 1
    }


@pytest.mark.asyncio
async def test_queued_response_does_not_need_another_checkpoint(monkeypatch):
    service = ResourceService(resource_processor=object())
    service._execute_resource_ingestion = AsyncMock(return_value={"status": "success"})
    get_tracker = AsyncMock(side_effect=AssertionError("response already durable in queue"))
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", get_tracker)
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/model",
        path="model.step",
        account_id="acme",
        user_id="alice",
        role="root",
        understanding_response_id="response-1",
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.ROOT)
    await service.execute_add_resource_job(
        msg, ctx=ctx, resource_lock=None, stage_callback=AsyncMock()
    )
    kwargs = service._execute_resource_ingestion.await_args.kwargs
    assert kwargs["understanding_response_id"] == "response-1"
    assert "_response_checkpoint" not in kwargs
    get_tracker.assert_not_called()


@pytest.mark.asyncio
async def test_checkpoint_failure_releases_reserved_target(monkeypatch):
    service = ResourceService(resource_processor=object())
    service._execute_resource_ingestion = AsyncMock()
    service._cleanup_reserved_target_if_empty = AsyncMock()
    tracker = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("task store unavailable")))
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/model",
        path="model.step",
        account_id="acme",
        user_id="alice",
        role="root",
        understanding_file_id="file-1",
        cleanup_empty_target_on_failure=True,
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.ROOT)
    lock = {"id": "lock-1"}
    with pytest.raises(RuntimeError, match="task store unavailable"):
        await service.execute_add_resource_job(
            msg, ctx=ctx, resource_lock=lock, stage_callback=AsyncMock()
        )
    service._execute_resource_ingestion.assert_not_called()
    service._cleanup_reserved_target_if_empty.assert_awaited_once_with(
        root_uri=msg.root_uri, ctx=ctx, resource_lock=lock
    )
