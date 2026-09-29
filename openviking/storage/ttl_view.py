# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Read-only TTL projection for public listings and object details."""

from openviking.config.ttl import resolve_ttl_config
from openviking.core.ttl import ttl_object_for_uri, ttl_scope_for_uri
from openviking.storage.abstract_overview import is_abstract_overview_uri
from openviking.storage.directory_ttl import read_directory_fields
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.config.ttl_config import TTLPolicy


def lifetime_fields(fields):
    return {"expires_at": fields.get("expires_at") or None, "ttl_days": fields.get("ttl_days")}


class TTLView:
    """Expose the effective owner deadline without modifying stored metadata."""

    def __init__(self, fs, ctx):
        self.fs, self.ctx = fs, ctx
        self._config = None
        self._owners = {}

    async def fields(self, uri, *, is_dir=False):
        scope = ttl_scope_for_uri(uri)
        if scope is None or is_abstract_overview_uri(uri):
            return lifetime_fields({})
        target = ttl_object_for_uri(uri)
        if target:
            root = target[1]
            if root not in self._owners:
                self._owners[root] = await read_directory_fields(self.fs, root, ctx=self.ctx)
            return lifetime_fields(self._owners[root])
        if is_dir:
            if self._config is None:
                self._config = (
                    await resolve_ttl_config(self.fs, self.ctx.account_id)
                    or get_openviking_config().ttl
                )
            policy = self._config.directories.get(uri.rstrip("/"), TTLPolicy())
            effective = self._config.resolve_uri_policy(uri, scope)
            return {
                **lifetime_fields({}),
                "ttl_days": policy.ttl_days,
                "policy": policy.model_dump(exclude_none=True),
                "effective_policy": effective.model_dump(exclude_none=True),
            }
        return lifetime_fields({})

    async def attach(self, entry):
        uri = entry.get("uri")
        return (
            {**entry, **await self.fields(uri, is_dir=entry.get("isDir", False))} if uri else entry
        )
