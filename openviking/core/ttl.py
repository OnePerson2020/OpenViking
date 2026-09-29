# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Resolve event-date and session directory lifetimes in one place.

TTL is off by default. Each events/YYYY/MM/DD directory or session owns one
expires_at shared by its L2 descendants. Relative deadlines use the latest
successful content update plus frozen ttl_days; absolute deadlines stay fixed.
L0/L1 summaries are retained independently of this lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from uuid import uuid4

from openviking.core.namespace import uri_parts
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.utils.config import TTLConfig, TTLScope, get_openviking_config

# Object-type tags used by lifecycle records / cleanup, kept next to the scope
# rules so callers do not re-derive them.
OBJECT_TYPE_EVENT = "event"
OBJECT_TYPE_SESSION = "session"
TTL_GENERATION_FIELD = "ttl_generation"
TTL_FIELD_NAMES = frozenset({"ttl_days", "received_at", "expires_at", TTL_GENERATION_FIELD})


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
    no lifecycle owner. Summary visibility is handled separately by readers.
    """
    scope = ttl_scope_for_uri(uri)
    if scope is None:
        return None
    try:
        parts = uri_parts(uri)
    except ValueError:
        return None
    if scope == "sessions":
        return (OBJECT_TYPE_SESSION, "viking://" + "/".join(parts[:4])) if len(parts) >= 4 else None
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


def resolve_ttl_days(uri: str, config: Optional[TTLConfig] = None) -> Optional[int]:
    """Resolve the effective ``ttl_days`` for a URI, or ``None`` when TTL is off."""
    scope = ttl_scope_for_uri(uri)
    if scope is None:
        return None
    ttl_config = config if config is not None else _current_ttl_config()
    if ttl_config is None:
        return None
    return ttl_config.resolve_uri(uri, scope)


def compute_expires_at(received_at: datetime, ttl_days: int) -> datetime:
    """Return the frozen expiry: ``received_at`` plus ``ttl_days`` whole days."""
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at + timedelta(days=ttl_days)


def session_content_updated_at(fields: Mapping[str, Any]) -> datetime:
    """Recover the latest content time without treating a TTL edit as a write."""
    timestamps = [
        parse_iso_datetime(fields[key])
        for key in ("received_at", "last_message_at", "created_at")
        if fields.get(key)
    ]
    # Older sessions renewed the deadline without updating received_at.
    if fields.get("ttl_days") and fields.get("expires_at"):
        timestamps.append(
            parse_iso_datetime(fields["expires_at"]) - timedelta(days=fields["ttl_days"])
        )
    if not timestamps:
        raise ValueError("session content update time is unavailable")
    return max(timestamps)


def freeze_ttl_fields(
    uri: str,
    *,
    received_at: Optional[datetime] = None,
    config: Optional[TTLConfig] = None,
) -> Optional[dict]:
    """Compute the initial TTL snapshot for a new object, or ``None`` when off.

    Returns a dict with RFC 3339 ``received_at``/``expires_at`` strings and the
    integer ``ttl_days`` actually applied. Later config changes do not alter the
    snapshot duration; relative objects renew it from successful content updates.
    """
    scope = ttl_scope_for_uri(uri)
    target = ttl_object_for_uri(uri)
    if target is None or target[1] != uri.rstrip("/"):
        return None
    ttl_config = config if config is not None else _current_ttl_config()
    if ttl_config is None:
        return None
    policy = ttl_config.resolve_uri_policy(uri, scope)
    ttl_days = policy.ttl_days if policy.mode == "days" else None
    received = received_at or datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    if policy.mode == "absolute":
        expires = datetime.fromtimestamp(policy.ttl_absolute, timezone.utc)
    elif ttl_days is not None:
        expires = compute_expires_at(received, ttl_days)
    else:
        return None
    return {
        "ttl_days": ttl_days,
        "received_at": format_iso8601(received),
        "expires_at": format_iso8601(expires),
        # An incarnation fence, not a policy field.  A URI delete/recreate gets
        # a new value so delayed cleanup/embedding work cannot touch the new
        # object.  Ordinary updates and session renewal preserve it.
        TTL_GENERATION_FIELD: str(uuid4()),
    }


def apply_ttl_fields(
    uri: str,
    metadata: Mapping[str, Any],
    *,
    existing_fields: Optional[Mapping[str, Any]] = None,
    received_at: Optional[datetime] = None,
    config: Optional[TTLConfig] = None,
) -> dict[str, Any]:
    """Return metadata with system-owned TTL fields created or renewed.

    On creation (``existing_fields is None``), caller-provided TTL fields are
    discarded and a snapshot is derived from the effective policy. On update,
    a relative snapshot is renewed from the successful content-update time
    while retaining its duration and incarnation fence. An explicit absolute
    deadline is copied verbatim. Public and LLM write paths therefore cannot
    choose expiry independently.
    """
    result = {key: value for key, value in metadata.items() if key not in TTL_FIELD_NAMES}
    target = ttl_object_for_uri(uri)
    if target is None or target[1] != uri.rstrip("/"):
        return result
    if existing_fields is None:
        snapshot = freeze_ttl_fields(uri, received_at=received_at, config=config)
        if snapshot:
            result.update(snapshot)
        return result
    existing = {
        field: existing_fields.get(field)
        for field in TTL_FIELD_NAMES
        if field in existing_fields and existing_fields.get(field) != ""
    }
    ttl_days = existing.get("ttl_days")
    # A manually adjusted deadline on an originally-relative object is absolute
    # from that point onward. Infer the legacy representation by verifying that
    # its stored deadline still exactly matches base + ttl_days. This preserves
    # compatibility without introducing a new persisted discriminator.
    relative_days = None
    if isinstance(ttl_days, int) and not isinstance(ttl_days, bool) and ttl_days > 0:
        try:
            base = parse_iso_datetime(str(existing["received_at"]))
            expiry = parse_iso_datetime(str(existing["expires_at"]))
            if compute_expires_at(base, ttl_days) == expiry:
                relative_days = ttl_days
        except (KeyError, TypeError, ValueError):
            relative_days = None
    if relative_days is not None:
        updated = received_at or datetime.now(timezone.utc)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        result.update(
            {
                "ttl_days": ttl_days,
                "received_at": format_iso8601(updated),
                "expires_at": format_iso8601(compute_expires_at(updated, relative_days)),
            }
        )
        generation = existing.get(TTL_GENERATION_FIELD)
        if generation:
            result[TTL_GENERATION_FIELD] = generation
        return result
    result.update(existing)
    if existing.get("expires_at"):
        # Preserve a fixed deadline while recording the latest content update,
        # so switching back to relative retention uses the correct base.
        result["ttl_days"] = None
        result["received_at"] = format_iso8601(received_at or datetime.now(timezone.utc))
    return result


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
    """Whether current policy creates TTL snapshots for new objects.

    This switch is deliberately not consulted by visibility checks. Policy
    changes only affect objects created afterwards; an object that already has
    a frozen ``expires_at`` must not become visible again when policy is later
    disabled.
    """
    config = _current_ttl_config()
    return config is not None and config.enabled


def hidden_by_ttl(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    """Whether a read/compute path should treat ``expires_at`` as logically gone.

    Used by filesystem reads and vector candidate validation against source
    metadata. Visibility follows the frozen object snapshot, not
    current policy: disabling TTL stops new snapshots but cannot revive an
    already-expired object. Objects without ``expires_at`` remain visible.
    """
    return is_expired(expires_at, now=now)


def _current_ttl_config() -> Optional[TTLConfig]:
    try:
        return get_openviking_config().ttl
    except Exception:
        # Config not initialized (e.g. unit tests, bootstrap). Fail closed to OFF.
        return None


def ttl_metadata_uri(object_type: str, uri: str) -> str:
    if object_type == OBJECT_TYPE_SESSION:
        return f"{uri}/.meta.json"
    if object_type == OBJECT_TYPE_EVENT:
        return f"{uri}/.ttl.json"
    raise ValueError(f"Unsupported TTL object type: {object_type}")
