"""SDK request timeouts are transient; query embeddings retry once, indexing keeps max_retries."""
import asyncio
import socket

import httpx
import pytest

from openviking.models.embedder import volcengine_embedders as ve
from openviking.utils.model_retry import is_retryable_api_error


def _silent_embedder(monkeypatch):
    server = socket.socket()  # accepts connections, never answers
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    monkeypatch.setattr(ve, "_EMBEDDING_REQUEST_TIMEOUT", httpx.Timeout(0.3))
    embedder = ve.VolcengineDenseEmbedder(
        model_name="m",
        api_key="k",
        api_base=f"http://127.0.0.1:{server.getsockname()[1]}/api/v3",
        dimension=8,
        input_type="text",
        config={"max_retries": 3},
    )
    attempts = []
    create = ve._create_async_ark

    async def count(_request):
        attempts.append(1)

    def counting(kwargs):
        client = create(kwargs)
        client._client.event_hooks["request"].append(count)
        return client

    monkeypatch.setattr(ve, "_create_async_ark", counting)
    monkeypatch.setattr("openviking.utils.model_retry._compute_delay", lambda *a, **k: 0)
    return server, embedder, attempts


@pytest.mark.parametrize("is_query, expected_attempts", [(True, 2), (False, 4)])
def test_timeout_is_retried_within_the_callers_budget(monkeypatch, is_query, expected_attempts):
    server, embedder, attempts = _silent_embedder(monkeypatch)
    with server, pytest.raises(RuntimeError) as failure:
        asyncio.run(embedder.embed_async("hi", is_query=is_query))
    assert "timed out" in str(failure.value)
    assert is_retryable_api_error(failure.value)
    assert len(attempts) == expected_attempts
