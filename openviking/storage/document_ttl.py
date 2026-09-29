# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""TTL queries and explicit lifetime edits for directory lifecycle owners."""

from uuid import uuid4

from openviking.core.ttl import (
    OBJECT_TYPE_SESSION,
    TTL_FIELD_NAMES,
    compute_expires_at,
    hidden_by_ttl,
    session_content_updated_at,
    ttl_object_for_uri,
    ttl_scope_for_uri,
)
from openviking.server.error_mapping import is_storage_not_found
from openviking.storage.acl import AclAction
from openviking.storage.directory_ttl import read_directory_fields, write_directory_fields
from openviking.storage.ttl_view import TTLView, lifetime_fields
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.exceptions import ConflictError, InvalidArgumentError, NotFoundError
from openviking_cli.utils.config.ttl_config import DocumentTTL


def _public_fields(uri, fields):
    return {
        "uri": uri,
        **{key: value for key, value in fields.items() if key in TTL_FIELD_NAMES},
        **lifetime_fields(fields),
    }


async def get_document_ttl(fs, uri, *, ctx):
    stat = await fs.stat(uri, ctx=ctx)
    if ttl_scope_for_uri(uri) is None:
        raise InvalidArgumentError("TTL only supports events and sessions")
    target = ttl_object_for_uri(uri)
    if target and target[1] == uri:
        return _public_fields(uri, await read_directory_fields(fs, uri, ctx=ctx))
    return {"uri": uri, **await TTLView(fs, ctx).fields(uri, is_dir=stat.get("isDir", False))}


async def update_document_expiry(fs, uri, expires_at=None, *, ctx, ttl_relative=None):
    target = ttl_object_for_uri(uri)
    stat = await fs.stat(uri, ctx=ctx)
    if target is None or target[1] != uri or not stat.get("isDir"):
        raise InvalidArgumentError(
            "TTL edits require an event date directory or a session directory"
        )
    if target[0] == OBJECT_TYPE_SESSION and expires_at is not None:
        raise InvalidArgumentError("Session directories support relative retention only")
    try:
        values = DocumentTTL(
            expires_at=expires_at,
            **({"ttl_relative": ttl_relative} if ttl_relative is not None else {}),
        )
        expiry = (
            format_iso8601(parse_iso_datetime(values.expires_at)) if values.expires_at else None
        )
        if expiry and hidden_by_ttl(expiry):
            raise ValueError("expires_at must be in the future")
    except (ValueError, TypeError) as exc:
        raise InvalidArgumentError(str(exc)) from exc
    await fs._ensure_access(uri, ctx, action=AclAction.WRITE)
    original = await read_directory_fields(fs, uri, ctx=ctx)
    lease = await fs._async_agfs.pathlock_acquire_tree(
        fs._uri_to_path(uri, ctx=ctx), timeout_secs=30.0
    )
    try:
        # Deletion or recreation may finish while this request waits for the
        # directory lock. An edit must still address the same live owner.
        try:
            stat = await fs._async_agfs.stat(fs._uri_to_path(uri, ctx=ctx), bypass_cache=True)
        except Exception as exc:
            if is_storage_not_found(exc):
                raise NotFoundError(uri, "directory") from exc
            raise
        if not stat.get("isDir"):
            raise InvalidArgumentError("TTL edits require a directory")
        fields = await read_directory_fields(fs, uri, ctx=ctx)
        if fields.get("ttl_generation") != original.get("ttl_generation"):
            raise ConflictError("directory changed while updating its expiry; reload and retry")
        if hidden_by_ttl(fields.get("expires_at")):
            raise NotFoundError(uri, "directory")
        if target[0] == OBJECT_TYPE_SESSION:
            updated = session_content_updated_at(fields)
        elif fields.get("received_at"):
            updated = parse_iso_datetime(fields["received_at"])
        else:
            # Explicit adoption uses the existing directory's content mtime.
            from datetime import datetime, timezone

            mtime = fs._ls_entry_mtime(stat)
            if mtime is None:
                raise InvalidArgumentError("directory update time is unavailable")
            updated = datetime.fromtimestamp(mtime, timezone.utc)
        if ttl_relative is not None:
            expiry = format_iso8601(compute_expires_at(updated, ttl_relative))
        fields.update(
            ttl_days=ttl_relative,
            received_at=format_iso8601(updated),
            expires_at=expiry,
            ttl_generation=fields.get("ttl_generation") or str(uuid4()),
        )
        if target[0] == OBJECT_TYPE_SESSION:
            fields["ttl_relative"] = ttl_relative
        await write_directory_fields(fs, uri, fields, ctx=ctx, lease_ref=lease)
        return _public_fields(uri, fields)
    finally:
        await fs._async_agfs.pathlock_release(lease)
