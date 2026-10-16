"""Embedding clients keep idle connections for 300 s instead of httpx's 5 s."""
from openviking.models.embedder import volcengine_embedders as ve


def test_ark_clients_keep_idle_connections():
    for make in (ve._create_sync_ark, ve._create_async_ark):
        client = make({"api_key": "k", "base_url": "https://ark.example.com/api/v3"})
        assert client._client._transport._pool._keepalive_expiry == 300.0
