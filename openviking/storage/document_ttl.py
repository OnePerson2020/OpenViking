# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Explicit retention edits for live files and sessions."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from openviking.core.ttl import (
    OBJECT_TYPE_EVENT,
    OBJECT_TYPE_RESOURCE_FILE,
    OBJECT_TYPE_SESSION,
    OBJECT_TYPE_SESSION_FILE,
    TTL_FIELD_NAMES,
    compute_expires_at,
    hidden_by_ttl,
    session_content_updated_at,
    ttl_object_for_uri,
    ttl_scope_for_uri,
)
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.storage.acl import AclAction
from openviking.storage.internal_names import (
    WEBDAV_RESERVED_FILENAMES,
    is_storage_internal_name,
    is_ttl_metadata_name,
)
from openviking.storage.resource_ttl import read_resource_fields, write_resource_fields
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.exceptions import ConflictError, InvalidArgumentError
from openviking_cli.utils.config.ttl_config import ContentTTL, TTLPolicy


async def _document_target(fs, uri, *, ctx):
    name = uri.rsplit("/", 1)[-1]
    if (
        name in WEBDAV_RESERVED_FILENAMES
        or is_storage_internal_name(name)
        or is_ttl_metadata_name(name)
    ):
        raise InvalidArgumentError("TTL can only be set on event or resource content files")
    stat = await fs.stat(uri, ctx=ctx)
    scope = ttl_scope_for_uri(uri)
    if scope == "resources":
        if stat.get("isDir"):
            raise InvalidArgumentError(
                "resource directories define defaults via resources/config; "
                "resources/ttl requires a file"
            )
        fields = await read_resource_fields(fs, OBJECT_TYPE_RESOURCE_FILE, uri, ctx=ctx)
        return OBJECT_TYPE_RESOURCE_FILE, fields or {}, stat
    if ttl_object_for_uri(uri) == (OBJECT_TYPE_SESSION, uri):
        fields = json.loads(await fs.read_file(f"{uri}/.meta.json", ctx=ctx))
        return OBJECT_TYPE_SESSION, fields, stat
    if scope == "sessions":
        from openviking.storage.session_file_ttl import (
            is_session_content,
            session_file_fields,
            session_root,
        )

        if stat.get("isDir") and session_root(uri):
            from openviking.server.error_mapping import is_storage_not_found

            try:
                raw = await fs._async_agfs.read(fs._uri_to_path(uri + "/.ttl.json", ctx=ctx))
                fields = json.loads(fs._handle_agfs_read(raw))
            except Exception as exc:
                if not is_storage_not_found(exc):
                    raise
                fields = {}
            return "session_directory", fields, stat
        if is_session_content(uri):
            fields = await session_file_fields(fs, uri, ctx=ctx)
            return OBJECT_TYPE_SESSION_FILE, fields or {}, stat
    if ttl_object_for_uri(uri, is_dir=bool(stat.get("isDir"))) == (OBJECT_TYPE_EVENT, uri):
        memory = MemoryFileUtils.read(await fs.read_file(uri, ctx=ctx), uri=uri)
        return (
            OBJECT_TYPE_EVENT,
            {key: value for key, value in memory.extra_fields.items() if key in TTL_FIELD_NAMES},
            stat,
        )
    raise InvalidArgumentError("uri must identify an event file, resource document or session")


def _public_fields(uri, fields):
    # Watch fingerprints and other lifecycle bookkeeping are private.
    return {"uri": uri, **{key: value for key, value in fields.items() if key in TTL_FIELD_NAMES}}


async def get_document_ttl(fs, uri: str, *, ctx) -> dict:
    """Read a live file or session's retention snapshot."""
    kind, fields, _ = await _document_target(fs, uri, ctx=ctx)
    if kind == "session_directory":
        return _directory_fields(uri, fields)
    return _public_fields(uri, fields)


def _directory_fields(uri, fields):
    policy = (
        fields
        if "mode" in fields
        else ({"mode": "days", **fields} if fields else {"mode": "inherit"})
    )
    return {**_public_fields(uri, fields), "policy": policy}


async def update_document_expiry(
    fs,
    uri: str,
    expires_at: str | None = None,
    *,
    ctx,
    ttl_relative: int | None = None,
    policy: dict | TTLPolicy | None = None,
) -> dict:
    """Set retention under the object's source lock, even when global TTL is off."""
    try:
        values = {"expires_at": expires_at, "policy": policy}
        if ttl_relative is not None:
            values["ttl_relative"] = ttl_relative
        policy = ContentTTL(**values)
        expiry = (
            format_iso8601(parse_iso_datetime(policy.expires_at))
            if policy.expires_at is not None
            else None
        )
        if expiry is not None and hidden_by_ttl(expiry):
            raise ValueError("expires_at must be in the future")
    except (ValueError, TypeError) as exc:
        raise InvalidArgumentError(str(exc)) from exc
    kind, original, _ = await _document_target(fs, uri, ctx=ctx)
    if policy.policy is not None and kind != "session_directory":
        raise InvalidArgumentError(
            "inherit/disabled directory policies require a session subdirectory"
        )
    if kind in {OBJECT_TYPE_SESSION, "session_directory"} and policy.expires_at is not None:
        raise InvalidArgumentError("session directories support relative retention only")
    await fs._ensure_access(uri, ctx, action=AclAction.WRITE)
    acquire = (
        fs._async_agfs.pathlock_acquire_tree
        if ttl_scope_for_uri(uri) == "sessions"
        else fs._async_agfs.pathlock_acquire_exact
    )
    from openviking.storage.session_file_ttl import session_root

    lock_uri = session_root(uri) or uri
    # A retention edit may race with an ordinary content update. Wait for the
    # current writer, then re-read its snapshot while holding the same lock.
    lease = await acquire(fs._uri_to_path(lock_uri, ctx=ctx), timeout_secs=30.0)
    try:
        live_kind, fields, stat = await _document_target(fs, uri, ctx=ctx)
        if (live_kind, fields.get("ttl_generation")) != (kind, original.get("ttl_generation")):
            raise ConflictError("document changed while updating its expiry; reload and retry")
        if kind in {OBJECT_TYPE_SESSION_FILE, "session_directory"}:
            from openviking.storage.session_file_ttl import (
                migrate_session_files,
                read_session_metadata,
            )

            metadata = await read_session_metadata(fs, lock_uri, ctx=ctx)
            await migrate_session_files(fs, lock_uri, metadata, ctx=ctx, lease_ref=lease)
            if kind == "session_directory":
                fields = (
                    policy.policy.model_dump(exclude_none=True)
                    if policy.policy is not None
                    else {"mode": "days", "ttl_days": policy.ttl_relative}
                )
                await fs.write_file(
                    uri + "/.ttl.json", json.dumps(fields), ctx=ctx, lease_ref=lease
                )
                return _directory_fields(uri, fields)
            from openviking.storage.session_file_ttl import session_file_fields

            fields = await session_file_fields(fs, uri, ctx=ctx)
        elif kind == OBJECT_TYPE_SESSION and fields.get("ttl_per_file"):
            fields["ttl_days"] = policy.ttl_relative
            fields["ttl_relative"] = policy.ttl_relative
            await fs.write_file(uri + "/.meta.json", json.dumps(fields), ctx=ctx, lease_ref=lease)
            return _public_fields(uri, fields)
        # Retention edits do not count as content updates. Preserve the saved
        # content timestamp; unmanaged files start from the storage modification time.
        if kind == OBJECT_TYPE_SESSION:
            try:
                updated = session_content_updated_at(fields)
            except ValueError as exc:
                raise InvalidArgumentError(str(exc)) from exc
        elif fields.get("received_at"):
            updated = parse_iso_datetime(fields["received_at"])
        else:
            mtime = fs._ls_entry_mtime(stat)
            if mtime is None:
                raise InvalidArgumentError("document content update time is unavailable")
            updated = datetime.fromtimestamp(mtime, timezone.utc)
        if policy.ttl_relative is not None:
            expiry = format_iso8601(compute_expires_at(updated, policy.ttl_relative))
        fields.update(
            ttl_days=policy.ttl_relative,
            received_at=format_iso8601(updated),
            expires_at=expiry,
            ttl_generation=fields.get("ttl_generation") or str(uuid4()),
        )
        if kind == OBJECT_TYPE_EVENT:
            memory = MemoryFileUtils.read(await fs.read_file(uri, ctx=ctx), uri=uri)
            memory.extra_fields.update(fields)
            await fs.write_file(uri, MemoryFileUtils.write(memory), ctx=ctx, lease_ref=lease)
        elif kind == OBJECT_TYPE_SESSION:
            fields["ttl_relative"] = policy.ttl_relative
            await fs.write_file(f"{uri}/.meta.json", json.dumps(fields), ctx=ctx, lease_ref=lease)
        else:
            await write_resource_fields(fs, kind, uri, fields, ctx=ctx, lease_ref=lease)
    finally:
        await fs._async_agfs.pathlock_release(lease)
    # A shorter relative duration may make the file due immediately.
    return _public_fields(uri, fields)
