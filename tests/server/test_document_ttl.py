# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""TTL configuration and live-document changes through the public HTTP surface."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openviking.core import ttl
from openviking.service.ttl_cleanup import TTLCleanupService
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.utils.config import get_openviking_config, set_openviking_config
from tests.storage.test_transfer_merge_binding import root_ctx
from tests.unit.service.test_ttl_cleanup import _cleanup_once

ROOT = "viking://user/default"
CONFIG = "/api/v1/admin/accounts/default/configuration"


@pytest.fixture(autouse=True)
def restore_config():
    original = get_openviking_config()
    yield
    set_openviking_config(original)


@pytest.fixture
async def ttl_admin_app(app):
    # ASGI test apps intentionally omit the auth lifespan. Match the existing
    # settings tests' admin gate while exercising the real config/storage stack.
    app.state.api_key_manager = SimpleNamespace(
        refresh_accounts_from_store=AsyncMock(),
        refresh_account_users_from_store=AsyncMock(),
        ensure_account_active=lambda account: None,
        get_accounts=lambda: [{"account_id": "default"}],
    )
    return app


@pytest.fixture
async def client(ttl_admin_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ttl_admin_app), base_url="http://testserver"
    ) as client:
        yield client


async def request(client, method, path, **kwargs):
    response = await getattr(client, method)(path, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["result"]


async def write(client, uri):
    await request(
        client,
        "post",
        "/api/v1/content/write",
        json={
            "uri": uri,
            "content": "Keep this document.",
            "mode": "create",
            "processing_mode": "vectors_only",
            "wait": True,
        },
    )
    return await request(client, "get", "/api/v1/content/ttl", params={"uri": uri})


async def rewrite(client, uri, *, mode):
    return await request(
        client,
        "post",
        "/api/v1/content/write",
        json={
            "uri": uri,
            "content": " Updated.",
            "mode": mode,
            "processing_mode": "vectors_only",
            "wait": True,
        },
    )


@pytest.mark.asyncio
async def test_session_retention_edit_and_append_use_latest_content_time(
    client, service, monkeypatch
):
    import ast

    from openviking.server import mcp_endpoint
    from openviking.session import session as session_module
    from openviking_cli.client.http import AsyncHTTPClient

    await request(client, "post", "/api/v1/sessions", json={"session_id": "manual-ttl"})
    uri = ROOT + "/sessions/manual-ttl"
    meta_uri = uri + "/.meta.json"
    before = json.loads(await service.viking_fs.read_file(meta_uri, ctx=root_ctx()))
    sdk = AsyncHTTPClient(url="http://testserver", account="default", user="default")
    sdk._http = client
    first = await sdk.update_ttl(uri, ttl_relative=7)
    original_record = await service.viking_fs.ttl_registry.get("default", uri)
    assert first["ttl_days"] == 7
    assert parse_iso_datetime(first["expires_at"]) - parse_iso_datetime(
        first["received_at"]
    ) == timedelta(days=7)
    changed_at = format_iso8601(parse_iso_datetime(first["received_at"]) + timedelta(days=1))
    monkeypatch.setattr(session_module, "get_current_timestamp", lambda: changed_at)
    await request(
        client,
        "post",
        "/api/v1/sessions/manual-ttl/messages",
        json={"role": "user", "content": "A new message renews retention."},
    )
    renewed = await request(client, "get", "/api/v1/content/ttl", params={"uri": uri})
    assert renewed["received_at"] == changed_at
    assert parse_iso_datetime(renewed["expires_at"]) - parse_iso_datetime(
        first["expires_at"]
    ) == timedelta(days=1)
    assert renewed["ttl_generation"] == first["ttl_generation"]
    token = mcp_endpoint._mcp_ctx.set(root_ctx())
    try:
        revised = ast.literal_eval(await mcp_endpoint.update_ttl(uri, ttl_relative=30))
    finally:
        mcp_endpoint._mcp_ctx.reset(token)
    assert revised["received_at"] == changed_at
    assert parse_iso_datetime(revised["expires_at"]) - parse_iso_datetime(changed_at) == timedelta(
        days=30
    )
    after = json.loads(await service.viking_fs.read_file(meta_uri, ctx=root_ctx()))
    assert after["created_at"] == before["created_at"]
    assert after["message_count"] == 1
    assert (await service.viking_fs.ttl_registry.get("default", uri)).expires_at == revised[
        "expires_at"
    ]
    # Configuration-only changes and failed content writes cannot renew TTL.
    monkeypatch.setattr(
        session_module,
        "get_current_timestamp",
        lambda: format_iso8601(parse_iso_datetime(changed_at) + timedelta(days=1)),
    )
    await request(
        client,
        "patch",
        "/api/v1/sessions/manual-ttl/config",
        json={"memory_extraction_config": {"events": {"tags": ["updated-config"]}}},
    )
    assert await sdk.get_ttl(uri) == revised
    with monkeypatch.context() as failure:
        failure.setattr(
            service.viking_fs,
            "append_file",
            AsyncMock(side_effect=OSError("storage temporarily unavailable")),
        )
        failed = await client.post(
            "/api/v1/sessions/manual-ttl/messages",
            json={"role": "user", "content": "Must not renew TTL"},
        )
        assert failed.status_code == 500
    assert await sdk.get_ttl(uri) == revised
    persisted = json.loads(await service.viking_fs.read_file(meta_uri, ctx=root_ctx()))
    assert persisted["message_count"] == 1
    # Directory edits are relative only and cannot silently switch to a fixed deadline.
    response = await client.patch(
        "/api/v1/content/ttl", json={"uri": uri, "expires_at": "2999-01-01T00:00:00Z"}
    )
    assert response.status_code == 400
    assert await sdk.get_ttl(uri) == revised
    summary_uri = uri + "/.abstract.md"
    await service.viking_fs.write_file(summary_uri, "Retained summary", ctx=root_ctx())
    real_expired = ttl.is_expired
    now = parse_iso_datetime(first["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(ttl, "is_expired", lambda value, **_: real_expired(value, now=now))
    cleanup = TTLCleanupService(service=service, service_loop=asyncio.get_running_loop())
    assert (await _cleanup_once(cleanup, original_record))["skipped"] == "renewed"
    now = parse_iso_datetime(revised["expires_at"]) + timedelta(days=2)
    assert (await client.get("/api/v1/sessions/manual-ttl")).status_code == 404
    assert (
        await client.get("/api/v1/content/read", params={"uri": uri + "/messages.jsonl"})
    ).status_code == 404
    response = await client.patch("/api/v1/content/ttl", json={"uri": uri, "ttl_relative": 60})
    assert response.status_code == 404
    record = await service.viking_fs.ttl_registry.get("default", uri)
    assert (await _cleanup_once(cleanup, record))["deleted"]
    assert await service.viking_fs.exists(uri, ctx=root_ctx())
    assert await service.viking_fs.read_file(summary_uri, ctx=root_ctx()) == "Retained summary"
    assert not await service.viking_fs.exists(
        uri + "/messages.jsonl", ctx=root_ctx(), include_expired=True
    )


@pytest.mark.asyncio
async def test_session_retention_requires_owner_access(client, ttl_admin_app, service):
    from openviking.server.auth import get_request_context
    from openviking.server.identity import RequestContext, Role
    from openviking_cli.session.user_id import UserIdentifier

    await request(client, "post", "/api/v1/sessions", json={"session_id": "private-ttl"})
    uri = ROOT + "/sessions/private-ttl"
    stranger = RequestContext(user=UserIdentifier("default", "stranger"), role=Role.USER)
    ttl_admin_app.dependency_overrides[get_request_context] = lambda: stranger
    try:
        response = await client.patch("/api/v1/content/ttl", json={"uri": uri, "ttl_relative": 7})
        assert response.status_code == 403, response.text
        response = await client.get("/api/v1/content/ttl", params={"uri": uri})
        assert response.status_code == 403, response.text
    finally:
        ttl_admin_app.dependency_overrides.pop(get_request_context)
    assert await service.viking_fs.ttl_registry.get("default", uri) is None


@pytest.mark.asyncio
async def test_session_ttl_formal_api_create_patch_omit_and_inherit(client, service, monkeypatch):
    from openviking_sdk.client import AsyncHTTPClient

    from openviking.session import session as session_module

    await request(
        client,
        "patch",
        CONFIG,
        json={"settings": {"ttl": {"sessions": {"mode": "days", "ttl_days": 30}}}},
    )
    sdk = AsyncHTTPClient(url="http://testserver", account="default", user="default")
    sdk._http = client
    await sdk.create_session("formal-ttl", options={"ttl_relative": 7})
    created = await sdk.get_session("formal-ttl")
    assert created["ttl_relative"] == created["ttl_days"] == 7
    received = created["received_at"]
    assert parse_iso_datetime(created["expires_at"]) - parse_iso_datetime(received) == timedelta(
        days=7
    )
    await sdk.update_session_config("formal-ttl", {})
    assert (await sdk.get_session("formal-ttl"))["expires_at"] == created["expires_at"]
    await sdk.update_session_config("formal-ttl", {"ttl_relative": 14})
    changed = await sdk.get_session("formal-ttl")
    assert changed["ttl_relative"] == changed["ttl_days"] == 14
    assert changed["received_at"] == received
    await sdk.update_session_config("formal-ttl", {"ttl_relative": None})
    inherited = await sdk.get_session("formal-ttl")
    assert inherited["ttl_relative"] is None and inherited["ttl_days"] == 30
    assert inherited["received_at"] == received
    fs, ctx = service.viking_fs, root_ctx()
    body = inherited["uri"] + "/attachments/old.txt"
    await fs.write_file(body, "old attachment", ctx=ctx)
    day29 = parse_iso_datetime(received) + timedelta(days=29)
    monkeypatch.setattr(session_module, "get_current_timestamp", lambda: format_iso8601(day29))
    await request(
        client,
        "post",
        "/api/v1/sessions/formal-ttl/messages",
        json={"role": "user", "content": "unrelated"},
    )
    renewed = await sdk.get_session("formal-ttl")
    assert parse_iso_datetime(renewed["expires_at"]) == day29 + timedelta(days=30)
    assert (await request(client, "get", "/api/v1/content/ttl", params={"uri": body}))[
        "expires_at"
    ] == renewed["expires_at"]
    # Inherit a disabled default: remove root expiry/registry without reviving expired data.
    await request(
        client, "patch", CONFIG, json={"settings": {"ttl": {"sessions": {"mode": "disabled"}}}}
    )
    await sdk.update_session_config("formal-ttl", {"ttl_relative": None})
    disabled = await sdk.get_session("formal-ttl")
    assert not disabled.get("expires_at") and not disabled.get("ttl_days")
    assert await fs.ttl_registry.get(ctx.account_id, inherited["uri"]) is None
    for name, options in [("implicit", {}), ("null", {"ttl_relative": None})]:
        await sdk.create_session(name, options=options)
        assert not (await sdk.get_session(name)).get("expires_at")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ttl_relative": True},
        {"ttl_relative": "7"},
        {"ttl_relative": 0},
        {"ttl_relative": -1},
        {"ttl_relative": 365001},
        {"ttl_absolute": 2000000000},
    ],
)
async def test_session_ttl_invalid_formal_fields_return_400(client, payload):
    created = await client.post("/api/v1/sessions", json=payload)
    assert created.status_code == 400, created.text
    changed = await client.patch("/api/v1/sessions/invalid/config", json=payload)
    assert changed.status_code == 400, changed.text


@pytest.mark.asyncio
async def test_session_ttl_expired_cannot_be_reset_or_extended(client, service, monkeypatch):
    await request(
        client, "post", "/api/v1/sessions", json={"session_id": "expired-config", "ttl_relative": 1}
    )
    before = await request(client, "get", "/api/v1/sessions/expired-config")
    now = parse_iso_datetime(before["expires_at"]) + timedelta(seconds=1)
    actual = ttl.is_expired
    monkeypatch.setattr(ttl, "is_expired", lambda value, **_: actual(value, now=now))
    for value in [30, None]:
        response = await client.patch(
            "/api/v1/sessions/expired-config/config", json={"ttl_relative": value}
        )
        assert response.status_code == 404
    raw = service.viking_fs._handle_agfs_read(
        await service.viking_fs._async_agfs.read(
            service.viking_fs._uri_to_path(before["uri"] + "/.meta.json", ctx=root_ctx())
        )
    )
    assert json.loads(raw)["expires_at"] == before["expires_at"]


@pytest.mark.asyncio
async def test_mcp_session_ttl_uses_formal_creation_and_config(client, monkeypatch):
    from openviking.server import mcp_endpoint as endpoint

    monkeypatch.setattr(endpoint, "_get_ctx", root_ctx)
    await endpoint.create_session("mcp-ttl-config", ttl_relative=7)
    created = await request(client, "get", "/api/v1/sessions/mcp-ttl-config")
    assert created["ttl_days"] == 7
    await endpoint.update_session_config("mcp-ttl-config", {"ttl_relative": 14})
    assert (await request(client, "get", "/api/v1/sessions/mcp-ttl-config"))["ttl_days"] == 14
    await endpoint.update_session_config("mcp-ttl-config", {})
    assert (await request(client, "get", "/api/v1/sessions/mcp-ttl-config"))["ttl_days"] == 14
    await endpoint.update_session_config("mcp-ttl-config", {"ttl_relative": None})
    result = await request(client, "get", "/api/v1/sessions/mcp-ttl-config")
    assert result["ttl_relative"] is None and not result.get("expires_at")


@pytest.mark.asyncio
async def test_session_default_does_not_adopt_unmanaged_but_explicit_override_does(client):
    await request(client, "post", "/api/v1/sessions", json={"session_id": "unmanaged"})
    await request(
        client,
        "patch",
        CONFIG,
        json={"settings": {"ttl": {"sessions": {"mode": "days", "ttl_days": 30}}}},
    )
    await request(
        client,
        "post",
        "/api/v1/sessions/unmanaged/messages",
        json={"role": "user", "content": "still unmanaged"},
    )
    assert not (await request(client, "get", "/api/v1/sessions/unmanaged")).get("expires_at")
    await request(client, "patch", "/api/v1/sessions/unmanaged/config", json={"ttl_relative": 7})
    assert (await request(client, "get", "/api/v1/sessions/unmanaged"))["ttl_days"] == 7


@pytest.mark.asyncio
async def test_event_directory_policy_and_response(client, service):
    parent = ROOT + "/memories/events"
    root = parent + "/2026/09/28"
    await request(
        client,
        "patch",
        CONFIG,
        json={
            "settings": {
                "ttl": {
                    "global": {"mode": "days", "ttl_days": 30},
                    "directories": {parent: {"mode": "days", "ttl_days": 7}},
                }
            }
        },
    )
    first = await write(client, root + "/first.md")
    assert first["ttl_days"] == 7
    second = await write(client, root + "/second.md")
    directory = await request(client, "get", "/api/v1/content/ttl", params={"uri": root})
    assert directory["expires_at"] == second["expires_at"]
    for uri in (root, root + "/first.md", root + "/second.md"):
        result = await request(client, "get", "/api/v1/fs/stat", params={"uri": uri})
        assert result["expires_at"] == directory["expires_at"]
        assert "ttl_per_file" not in result
    rows = await request(client, "get", "/api/v1/fs/ls", params={"uri": root})
    assert all(
        row["expires_at"] == directory["expires_at"]
        for row in rows
        if not row["uri"].rsplit("/", 1)[-1].startswith(".")
    )
    # Updating the directory's default cannot rewrite an existing bucket.
    await request(
        client,
        "patch",
        CONFIG,
        json={"settings": {"ttl": {"directories": {parent: {"mode": "days", "ttl_days": 14}}}}},
    )
    assert (await write(client, root + "/third.md"))["ttl_days"] == 7
    assert (await write(client, parent + "/2026/09/29/new.md"))["ttl_days"] == 14
    config_view = await request(client, "get", "/api/v1/fs/stat", params={"uri": parent})
    assert config_view["expires_at"] is None
    assert config_view["effective_policy"] == {"mode": "days", "ttl_days": 14}


@pytest.mark.asyncio
async def test_public_scope_excludes_resources_and_file_edits(client, service):
    await request(
        client, "post", "/api/v1/sessions", json={"session_id": "whole", "ttl_relative": 7}
    )
    root = ROOT + "/sessions/whole"
    await service.viking_fs.write_file(root + "/attachment.txt", "keep", ctx=root_ctx())
    for uri in (root + "/attachment.txt",):
        result = await client.patch("/api/v1/content/ttl", json={"uri": uri, "ttl_relative": 14})
        assert result.status_code == 400
    session = await request(client, "get", "/api/v1/sessions/whole")
    assert session["expires_at"]
    assert "ttl_per_file" not in session
    result = await client.patch(
        CONFIG, json={"settings": {"ttl": {"resources": {"mode": "days", "ttl_days": 7}}}}
    )
    assert result.status_code == 400
    result = await client.post("/api/v1/resources", json={"path": "a.md", "ttl_relative": 7})
    assert result.status_code == 400
    result = await client.patch(
        "/api/v1/resources/config", json={"uri": "viking://resources", "ttl_relative": 7}
    )
    assert result.status_code == 404
