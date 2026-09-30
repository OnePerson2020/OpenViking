# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Exercise TTL producer/consumer handoffs with the real native path locks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.pyagfs import AsyncAGFSClient
from openviking.storage.abstract_overview import semantic_body_digest
from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.utils.agfs_utils import RagfsBindingConfig, create_agfs_client
from openviking_cli.utils.config.agfs_config import AGFSConfig


@pytest.fixture
async def source_fs(tmp_path):
    client = create_agfs_client(
        RagfsBindingConfig(agfs=AGFSConfig(path=str(tmp_path), backend="local"))
    )
    agfs = AsyncAGFSClient(client)
    await agfs.mkdir("/local/default")
    await agfs.mkdir("/local/default/source")
    fs = SimpleNamespace(
        _async_agfs=agfs,
        _uri_to_path=lambda uri, ctx=None: "/local/default/source",
        read_file=AsyncMock(),
    )

    async def read(path, **kwargs):
        return (await fs.read_file(path)).encode()

    fs._async_agfs.read = read
    fs._handle_agfs_read = lambda raw: raw
    fs._ttl_uri_visible = AsyncMock(return_value=True)
    try:
        yield fs
    finally:
        client.close()


async def _handoff(fs, operation, *, before_release=lambda: None):
    producer = await fs._async_agfs.pathlock_acquire_exact(fs._uri_to_path(""))
    consumer = asyncio.create_task(operation())
    try:
        # A zero-timeout consumer fails while the producer still owns the lock.
        # A waiting consumer must not validate or write before that lock drops.
        await asyncio.sleep(0.1)
        assert not consumer.done()
        fs.read_file.assert_not_awaited()
        before_release()
    finally:
        await fs._async_agfs.pathlock_release(producer)
        try:
            result = await asyncio.wait_for(consumer, timeout=5)
        finally:
            if not consumer.done():
                consumer.cancel()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["original", "updated"])
async def test_summary_embedding_waits_and_rechecks_digest(source_fs, monkeypatch, body):
    fs = source_fs
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fs)
    handler = object.__new__(TextEmbeddingHandler)
    write = AsyncMock(return_value="vector-id")
    result = await _handoff(
        fs,
        lambda: handler._write_directory_vector_if_current(
            "viking://user/default/memories/events/.abstract.md",
            semantic_body_digest("original"),
            object(),
            write,
        ),
        before_release=lambda: setattr(fs.read_file, "return_value", body),
    )
    assert result == ("vector-id" if body == "original" else None)
    assert write.await_count == int(body == "original")
