# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Independent session file retention, with relative directory defaults.

Legacy sessions retain their whole-session lifecycle until a child is explicitly
configured. Migration snapshots every existing file before retiring the root
expiry, so a queued root cleanup cannot delete independently retained children.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from openviking.core.ttl import (
    OBJECT_TYPE_SESSION_FILE,
    TTL_FIELD_NAMES,
    apply_ttl_fields,
    compute_expires_at,
    hidden_by_ttl,
    ttl_scope_for_uri,
)
from openviking.server.error_mapping import is_storage_not_found
from openviking.storage.internal_names import (
    WEBDAV_RESERVED_FILENAMES,
    is_storage_internal_name,
    is_ttl_metadata_name,
)
from openviking.storage.resource_ttl import read_resource_fields, write_resource_fields
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.exceptions import NotFoundError

_CONTROL_NAMES = frozenset({".meta.json", ".done", ".failed.json", ".pending.json"})


def session_root(uri: str) -> str | None:
    if ttl_scope_for_uri(uri) != "sessions":
        return None
    parts = uri.removeprefix("viking://").rstrip("/").split("/")
    return "viking://" + "/".join(parts[:4]) if len(parts) >= 4 else None


def is_session_content(uri: str) -> bool:
    root = session_root(uri)
    name = uri.rsplit("/", 1)[-1]
    return bool(
        root
        and uri != root
        and name not in _CONTROL_NAMES
        and name not in WEBDAV_RESERVED_FILENAMES
        and not is_storage_internal_name(name)
        and not is_ttl_metadata_name(name)
    )


async def read_session_metadata(fs, root, *, ctx):
    try:
        await fs._async_agfs.stat(fs._uri_to_path(root + "/.meta.json", ctx=ctx), bypass_cache=True)
        raw = fs._handle_agfs_read(
            await fs._async_agfs.read(fs._uri_to_path(root + "/.meta.json", ctx=ctx))
        )
    except Exception as exc:
        if is_storage_not_found(exc):
            return {}
        raise
    metadata = json.loads(raw)
    if not isinstance(metadata, dict):
        raise ValueError(f"Invalid session metadata: {root}")
    return metadata


async def session_file_visible(fs, uri, *, ctx, metadata=None) -> bool:
    root = session_root(uri)
    if root is None:
        return True
    metadata = metadata if metadata is not None else await read_session_metadata(fs, root, ctx=ctx)
    if not metadata.get("ttl_per_file"):
        return not hidden_by_ttl(metadata.get("expires_at"))
    from openviking.core.ttl import ttl_object_for_uri

    target = ttl_object_for_uri(uri)
    if target and target[0] == OBJECT_TYPE_SESSION_FILE:
        uri = target[1]
    if not is_session_content(uri):
        return True
    fields = await session_file_fields(fs, uri, ctx=ctx)
    if fields:
        return not hidden_by_ttl(fields.get("expires_at"))
    record = await fs.ttl_registry.get(ctx.account_id, uri)
    return record is None or not hidden_by_ttl(record.expires_at)


async def migrate_session_files(fs, root, metadata, *, ctx, lease_ref):
    """Convert a live legacy root under its tree lease without moving deadlines."""
    if metadata.get("ttl_per_file"):
        return metadata
    if hidden_by_ttl(metadata.get("expires_at")):
        raise NotFoundError(root, "session")
    inherited = {key: value for key, value in metadata.items() if key in TTL_FIELD_NAMES}
    if inherited.get("ttl_days") and inherited.get("expires_at"):
        inherited["received_at"] = format_iso8601(
            parse_iso_datetime(inherited["expires_at"]) - timedelta(days=inherited["ttl_days"])
        )

    async def visit(directory):
        path = fs._uri_to_path(directory, ctx=ctx)
        for entry in await fs._ls_entries(path):
            name = entry.get("name", "")
            if not name or name in {".", ".."} or is_storage_internal_name(name):
                continue
            child = directory + "/" + name
            if entry.get("isDir"):
                await visit(child)
            elif is_session_content(child) and inherited.get("expires_at"):
                # A prior interrupted migration may have left a sidecar. The
                # root is still authoritative until the final marker is durable.
                await write_resource_fields(
                    fs,
                    OBJECT_TYPE_SESSION_FILE,
                    child,
                    {**inherited, "ttl_generation": str(uuid4())},
                    ctx=ctx,
                    lease_ref=lease_ref,
                )

    await visit(root)
    metadata = {**metadata, "ttl_per_file": True}
    metadata.pop("expires_at", None)
    await fs.write_file(root + "/.meta.json", json.dumps(metadata), ctx=ctx, lease_ref=lease_ref)
    return metadata


async def session_directory_days(fs, uri, metadata, *, ctx):
    from openviking.config.ttl import resolve_ttl_config
    from openviking.core.ttl import resolve_ttl_days

    directory = uri.rsplit("/", 1)[0]
    root = session_root(uri)
    while root and (directory == root or directory.startswith(root + "/")):
        if directory == root and "ttl_relative" in metadata:
            break  # Formal session config supersedes legacy root .ttl.json.
        try:
            raw = fs._handle_agfs_read(
                await fs._async_agfs.read(fs._uri_to_path(directory + "/.ttl.json", ctx=ctx))
            )
        except Exception as exc:
            if not is_storage_not_found(exc):
                raise
        else:
            from openviking_cli.utils.config.ttl_config import TTLPolicy

            fields = json.loads(raw)
            policy = TTLPolicy.model_validate(
                fields if "mode" in fields else {"mode": "days", **fields}
            )
            if policy.mode == "days":
                return policy.ttl_days
            if policy.mode == "disabled":
                return None
        if directory == root:
            break
        directory = directory.rsplit("/", 1)[0]
    if "ttl_relative" in metadata or "ttl_days" in metadata:
        return metadata.get("ttl_days")
    return resolve_ttl_days(uri, await resolve_ttl_config(fs, ctx.account_id))


async def prepare_session_file_update(fs, uri, content, *, ctx, lease_ref):
    """Calculate metadata before writing bytes; publish only on successful write."""
    if not is_session_content(uri):
        return None
    root = session_root(uri)
    metadata = await read_session_metadata(fs, root, ctx=ctx)
    if not metadata.get("ttl_per_file"):
        return None
    if not await session_file_visible(fs, uri, ctx=ctx, metadata=metadata):
        raise NotFoundError(uri, "session file")
    fields = await session_file_fields(fs, uri, ctx=ctx)
    now = datetime.now(timezone.utc)
    if fields:
        renewed = apply_ttl_fields(uri, {}, existing_fields=fields, received_at=now)
        return await _journal_update(
            fs, uri, fields, renewed, content, ctx=ctx, lease_ref=lease_ref
        )
    try:
        await fs._async_agfs.stat(fs._uri_to_path(uri, ctx=ctx), bypass_cache=True)
    except Exception as exc:
        if not is_storage_not_found(exc):
            raise
    else:
        return None  # A default change never adopts existing unmanaged files.
    days = await session_directory_days(fs, uri, metadata, ctx=ctx)
    if days is None:
        return None
    renewed = {
        "ttl_days": days,
        "received_at": format_iso8601(now),
        "expires_at": format_iso8601(compute_expires_at(now, days)),
        "ttl_generation": str(uuid4()),
    }
    return await _journal_update(fs, uri, None, renewed, content, ctx=ctx, lease_ref=lease_ref)


async def _journal_update(fs, uri, previous, desired, content, *, ctx, lease_ref):
    try:
        old = fs._handle_agfs_read(await fs._async_agfs.read(fs._uri_to_path(uri, ctx=ctx)))
    except Exception as exc:
        if not is_storage_not_found(exc):
            raise
        old = None
    if old == content:
        return None  # Identical bytes do not constitute a content change.
    pending = {"fields": desired, "sha256": hashlib.sha256(content).hexdigest()}
    journal = {**(previous or desired), "_ttl_pending": pending}
    # Retain the old deadline until bytes match this durable intent. Readers and
    # cleanup recover a successful write even when finalization is interrupted.
    await write_resource_fields(
        fs, OBJECT_TYPE_SESSION_FILE, uri, journal, ctx=ctx, lease_ref=lease_ref
    )
    return previous, desired


async def session_file_fields(fs, uri, *, ctx):
    root = session_root(uri)
    if root is None or not is_session_content(uri):
        return {}
    metadata = await read_session_metadata(fs, root, ctx=ctx)
    if not metadata.get("ttl_per_file"):
        return {key: value for key, value in metadata.items() if key in TTL_FIELD_NAMES}
    fields = await read_resource_fields(fs, OBJECT_TYPE_SESSION_FILE, uri, ctx=ctx)
    if fields is None:
        return {}
    pending = fields.pop("_ttl_pending", None)
    if pending:
        try:
            raw = fs._handle_agfs_read(await fs._async_agfs.read(fs._uri_to_path(uri, ctx=ctx)))
        except Exception as exc:
            if not is_storage_not_found(exc):
                raise
        else:
            if hashlib.sha256(raw).hexdigest() == pending["sha256"]:
                return pending["fields"]
    return fields


async def rollback_session_file_update(fs, uri, mutation, *, ctx, lease_ref):
    if mutation is None:
        return
    previous, desired = mutation
    if previous is not None:
        await write_resource_fields(
            fs, OBJECT_TYPE_SESSION_FILE, uri, previous, ctx=ctx, lease_ref=lease_ref
        )
    else:
        from openviking.core.ttl import ttl_metadata_uri

        metadata_uri = ttl_metadata_uri(OBJECT_TYPE_SESSION_FILE, uri)
        metadata_lease = await fs._async_agfs.pathlock_acquire_exact(
            fs._uri_to_path(metadata_uri, ctx=ctx), owner_lease_ref=lease_ref
        )
        try:
            await fs.remove_files(metadata_uri, ctx=ctx, lease_ref=metadata_lease)
        finally:
            await fs._async_agfs.pathlock_release(metadata_lease)
        await fs.ttl_registry.remove_if_generation(ctx.account_id, uri, desired["ttl_generation"])


async def complete_session_file_update(fs, uri, mutation, *, ctx, lease_ref):
    if mutation is not None:
        await write_resource_fields(
            fs, OBJECT_TYPE_SESSION_FILE, uri, mutation[1], ctx=ctx, lease_ref=lease_ref
        )
