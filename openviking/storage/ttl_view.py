# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Read-only TTL projection for public listings and object details."""

import json

from openviking.config.ttl import resolve_ttl_config
from openviking.core.ttl import OBJECT_TYPE_EVENT, ttl_object_for_uri, ttl_scope_for_uri
from openviking.server.error_mapping import is_storage_not_found
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.storage.abstract_overview import is_abstract_overview_uri
from openviking.storage.resource_ttl import resource_ttl_fields
from openviking.storage.session_file_ttl import (
    read_session_metadata,
    session_directory_days,
    session_file_fields,
    session_root,
)
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.config.ttl_config import TTLPolicy


def lifetime_fields(fields):
    """Keep null explicit at the API boundary, without changing stored metadata."""
    return {
        "expires_at": fields.get("expires_at") or None,
        "ttl_days": fields.get("ttl_days"),
    }


class TTLView:
    """Reuse configuration and session roots within a single response."""

    def __init__(self, fs, ctx):
        self.fs = fs
        self.ctx = ctx
        self._config = None
        self._sessions = {}

    async def fields(self, uri, *, is_dir=False):
        result = lifetime_fields({})
        scope = ttl_scope_for_uri(uri)
        if scope is None or is_abstract_overview_uri(uri):
            return result
        root = session_root(uri)
        if root:
            if root not in self._sessions:
                self._sessions[root] = await read_session_metadata(self.fs, root, ctx=self.ctx)
            metadata = self._sessions[root]
            if not metadata.get("ttl_per_file"):
                return lifetime_fields(metadata)
            if uri == root:
                days = await session_directory_days(
                    self.fs, root + "/__ttl_projection__", metadata, ctx=self.ctx
                )
                return {
                    **result,
                    "ttl_per_file": True,
                    "ttl_days": days,
                    "effective_policy": {"mode": "days", "ttl_days": days}
                    if days
                    else {"mode": "disabled"},
                }
            if not is_dir:
                return lifetime_fields(await session_file_fields(self.fs, uri, ctx=self.ctx))
            try:
                raw = await self.fs._async_agfs.read(
                    self.fs._uri_to_path(uri + "/.ttl.json", ctx=self.ctx)
                )
                stored = json.loads(self.fs._handle_agfs_read(raw))
            except Exception as exc:
                if not is_storage_not_found(exc):
                    raise
                stored = {"mode": "inherit"}
            policy = TTLPolicy.model_validate(
                stored if "mode" in stored else {"mode": "days", **stored}
            )
            days = await session_directory_days(
                self.fs, uri + "/__ttl_projection__", metadata, ctx=self.ctx
            )
            return {
                **result,
                "ttl_days": policy.ttl_days,
                "policy": policy.model_dump(exclude_none=True),
                "effective_policy": {"mode": "days", "ttl_days": days}
                if days
                else {"mode": "disabled"},
            }
        if is_dir:
            if self._config is None:
                self._config = (
                    await resolve_ttl_config(self.fs, self.ctx.account_id)
                    or get_openviking_config().ttl
                )
            policy = self._config.directories.get(uri.rstrip("/"), TTLPolicy())
            effective = self._config.resolve_uri_policy(
                uri.rstrip("/") + "/__ttl_projection__", scope
            )
            return {
                **result,
                "ttl_days": policy.ttl_days,
                "policy": policy.model_dump(exclude_none=True),
                "effective_policy": effective.model_dump(exclude_none=True),
            }
        if scope == "resources":
            return lifetime_fields(await resource_ttl_fields(self.fs, uri, ctx=self.ctx))
        target = ttl_object_for_uri(uri)
        if target and target[0] == OBJECT_TYPE_EVENT:
            memory = MemoryFileUtils.read(await self.fs.read_file(uri, ctx=self.ctx), uri=uri)
            return lifetime_fields(memory.extra_fields)
        return result

    async def attach(self, entry):
        uri = entry.get("uri")
        if uri:
            return {**entry, **await self.fields(uri, is_dir=entry.get("isDir", False))}
        return entry
