# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Resolve event-date and session directory lifetimes in one place.

TTL is off by default. Each events/YYYY/MM/DD directory or session owns one
expires_at shared by all descendants. Root policy changes apply to existing
directories; only sessions also renew after successful content updates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from openviking.core.namespace import uri_parts
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.utils.config import TTLConfig, TTLScope, get_openviking_config
from openviking_cli.utils.config.ttl_config import TTLPolicy

# Object-type tags used by lifecycle records / cleanup, kept next to the scope
# rules so callers do not re-derive them.
OBJECT_TYPE_EVENT = "event"
OBJECT_TYPE_SESSION = "session"
TTL_FIELD_NAMES = frozenset({"ttl_days", "received_at", "expires_at"})


def ttl_scope_for_uri(uri: str) -> Optional[TTLScope]:
    """Classify a canonical URI into a TTL scope, or ``None`` when unscoped.

    Only user events, peer events and sessions are in scope. Anything else
    (preferences, entities, skills, non-event memories, ...) returns
    ``None`` so TTL never touches it.
    """
    try:
        parts = uri_parts(uri)
    except ValueError:
        return None
    if len(parts) < 3 or parts[0] != "user":
        return None
    # sessions: viking://user/{uid}/sessions/...
    if parts[2] == "sessions":
        return "sessions"
    # peer events: viking://user/{uid}/peers/{pid}/memories/events/...
    if len(parts) >= 6 and parts[2] == "peers" and parts[4] == "memories" and parts[5] == "events":
        return "peer_events"
    # user events: viking://user/{uid}/memories/events/...
    if len(parts) >= 4 and parts[2] == "memories" and parts[3] == "events":
        return "user_events"
    return None


def ttl_object_for_uri(uri: str) -> Optional[tuple[str, str]]:
    """Map a lifecycle directory or descendant to its event-date/session owner.

    Upper policy containers and paths outside the standard date layout have
    no lifecycle owner. Summaries inside an owner share its lifetime.
    """
    scope = ttl_scope_for_uri(uri)
    if scope is None:
        return None
    try:
        parts = uri_parts(uri)
    except ValueError:
        return None
    if scope == "sessions":
        from openviking.storage.internal_names import (
            WEBDAV_RESERVED_FILENAMES,
            is_storage_internal_name,
        )

        if (
            len(parts) < 4
            or parts[3] in {*WEBDAV_RESERVED_FILENAMES, ".meta.json", ".ttl.json"}
            or is_storage_internal_name(parts[3])
        ):
            return None
        return OBJECT_TYPE_SESSION, "viking://" + "/".join(parts[:4])
    depth = 6 if scope == "peer_events" else 4
    if len(parts) < depth + 3:
        return None
    # Only the standard YYYY/MM/DD bucket is a lifecycle owner. A policy
    # container or an arbitrary event filename never becomes a TTL object.
    date_parts = parts[depth : depth + 3]
    if [len(part) for part in date_parts] != [4, 2, 2]:
        return None
    try:
        datetime.strptime("/".join(date_parts), "%Y/%m/%d")
    except ValueError:
        return None
    return OBJECT_TYPE_EVENT, "viking://" + "/".join(parts[: depth + 3])


def compute_expires_at(received_at: datetime, ttl_days: int) -> datetime:
    """Return ``received_at`` plus ``ttl_days`` whole days."""
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at + timedelta(days=ttl_days)


def policy_ttl_fields(policy: TTLPolicy, received_at: Optional[datetime] = None) -> dict:
    """Compute a directory's fields without replacing its business timestamp.

    Creation and explicit policy application use the same calculation. Policy
    application may shorten a deadline, unlike ordinary session renewal.
    """
    if received_at is not None and received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    days = policy.ttl_days if policy.mode == "days" else None
    if policy.mode == "absolute":
        expires = datetime.fromtimestamp(policy.ttl_absolute, timezone.utc)
    elif days is not None:
        if received_at is None:
            raise ValueError("relative TTL requires the original content timestamp")
        expires = compute_expires_at(received_at, days)
    else:
        expires = None
    fields = {
        "ttl_days": days,
        "expires_at": format_iso8601(expires) if expires is not None else "",
    }
    if received_at is not None:
        fields["received_at"] = format_iso8601(received_at)
    return fields


def initial_ttl_fields(
    uri: str,
    *,
    received_at: Optional[datetime] = None,
    config: Optional[TTLConfig] = None,
) -> Optional[dict]:
    """Initialize a new owner's deadline, or return ``None`` when TTL is off."""
    scope = ttl_scope_for_uri(uri)
    target = ttl_object_for_uri(uri)
    if target is None or target[1] != uri.rstrip("/"):
        return None
    ttl_config = config if config is not None else _current_ttl_config()
    if ttl_config is None:
        return None
    policy = ttl_config.resolve_uri_policy(uri, scope)
    if policy.mode not in {"days", "absolute"}:
        return None
    return policy_ttl_fields(policy, received_at or datetime.now(timezone.utc))


def strip_ttl_fields(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep TTL out of file payloads; the lifecycle directory owns it."""
    return {
        key: value
        for key, value in metadata.items()
        if key not in TTL_FIELD_NAMES and key != "ttl_generation"
    }


def is_expired(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    """Return whether an ``expires_at`` timestamp is at or past ``now`` (UTC).

    Absent/blank/unparseable expiry means "no TTL" and is never expired, matching
    the read barrier's absent-field-visible rule.
    """
    if not expires_at:
        return False
    try:
        expires = parse_iso_datetime(expires_at)
    except Exception:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expires <= current


def ttl_enabled() -> bool:
    """Whether policy enables TTL; reads always use persisted object deadlines."""
    config = _current_ttl_config()
    return config is not None and config.enabled


def hidden_by_ttl(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    """Whether a read/compute path should treat ``expires_at`` as logically gone.

    Used by filesystem reads and vector candidate validation against source
    metadata. Visibility follows the persisted object deadline. Policy updates
    can change a live object's deadline but cannot revive an expired object.
    """
    return is_expired(expires_at, now=now)


def _current_ttl_config() -> Optional[TTLConfig]:
    try:
        return get_openviking_config().ttl
    except Exception:
        # Config not initialized (e.g. unit tests, bootstrap). Fail closed to OFF.
        return None


def ttl_metadata_uri(object_type: str, uri: str) -> str:
    if object_type in {OBJECT_TYPE_SESSION, OBJECT_TYPE_EVENT}:
        return f"{uri}/.meta.json"
    raise ValueError(f"Unsupported TTL object type: {object_type}")
