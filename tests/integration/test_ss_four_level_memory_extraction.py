# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Live extraction test for SS project/plot/clip/user memories.

This test deliberately separates:

* ``expected``: the human-reviewed candidate ground truth built from the source session.
* ``actual``: files produced by the real SessionCommit -> ExtractLoop -> updater pipeline.

The test is opt-in because it uses the configured live VLM and embedding services.

Run from the repository root::

    RUN_SS_MEMORY_EXTRACTION_TEST=1 \
    SS_MEMORY_SESSION_FILE=result/ss_memory_ground_truth/sesn-.../session.json \
    SS_MEMORY_EXPECTED_MANIFEST=result/ss_memory_ground_truth/sesn-.../read_manifest.json \
    SS_MEMORY_ACTUAL_DIR=result/ss_memory_ground_truth/sesn-.../actual \
    SS_MEMORY_OUTPUT_FORMAT=json \
    SS_MEMORY_TYPES=ss_plot \
      .venv/bin/pytest tests/integration/test_ss_four_level_memory_extraction.py -v -s -m integration
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from openviking.message import TextPart
from openviking.server.config import load_server_config
from openviking.server.identity import RequestContext, Role
from openviking.service.core import OpenVikingService
from openviking.session.memory.utils import MemoryFileUtils
from openviking.telemetry import tracer
from openviking.telemetry.tracer import init_tracer_from_server_config
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.config_loader import load_json_config, resolve_config_path
from openviking_cli.utils.config.consts import (
    DEFAULT_OV_CONF,
    OPENVIKING_CONFIG_ENV,
)
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SCHEMAS = Path(__file__).parent / "fixtures" / "ss_memory_schemas"
DEFAULT_SAMPLE_ROOT = (
    PROJECT_ROOT / "result" / "ss_memory_ground_truth" / "sesn-20260918042348-c8t57"
)
DEFAULT_SOURCE = DEFAULT_SAMPLE_ROOT / "session.json"
DEFAULT_EXPECTED = DEFAULT_SAMPLE_ROOT / "read_manifest.json"
MEMORY_TYPES = {"ss_user", "ss_project", "ss_plot", "ss_clip"}
RESOURCE_ID_RE = re.compile(r"\bra_[A-Za-z0-9_]+\b")
TOOL_EVIDENCE_MAX_CHARS = 1_000
_SS_COMMAND_RE = re.compile(r"^\s*ss-cli\s+(?P<resource>[A-Za-z0-9_-]+)\s+(?P<verb>[A-Za-z0-9_-]+)")
_SS_FLAG_RE = re.compile(
    r"(?:^|\s)-{1,2}(?P<key>"
    r"project-id|plot-id|clip-id|take-id|resource-asset-id|resource-item-id|"
    r"resource-id|generation-task-id|status"
    r")\s+(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
)
_TOOL_FIELD_ALIASES = {
    "project_id": "project_id",
    "projectId": "project_id",
    "project-id": "project_id",
    "plot_id": "plot_id",
    "plotId": "plot_id",
    "plot-id": "plot_id",
    "clip_id": "clip_id",
    "clipId": "clip_id",
    "clip-id": "clip_id",
    "take_id": "take_id",
    "takeId": "take_id",
    "take-id": "take_id",
    "selected_take": "selected_take_id",
    "resource_asset_id": "resource_id",
    "resourceAssetId": "resource_id",
    "resource-asset-id": "resource_id",
    "output_resource_asset_id": "output_resource_id",
    "creative_asset_id": "creative_asset_id",
    "creativeAssetId": "creative_asset_id",
    "resource-id": "creative_asset_id",
    "resource_item_id": "resource_item_id",
    "resourceItemId": "resource_item_id",
    "resource-item-id": "resource_item_id",
    "generation_id": "generation_id",
    "generationTaskId": "generation_task_id",
    "generation-task-id": "generation_task_id",
    "source_snapshot_id": "snapshot_id",
    "snapshot_id": "snapshot_id",
    "source_job_id": "job_id",
    "task_id": "task_id",
    "version_id": "version_id",
    "current_version_id": "version_id",
    "clip_revision_id": "clip_revision_id",
    "plot_revision_id": "plot_revision_id",
    "profile_revision_id": "profile_revision_id",
    "status": "status",
    "job_status": "job_status",
    "processing_status": "processing_status",
    "generation_state": "generation_state",
    "lifecycle": "lifecycle",
}
_TOOL_IDENTITY_FIELDS = {
    "project_id",
    "plot_id",
    "clip_id",
    "take_id",
    "selected_take_id",
    "resource_id",
    "output_resource_id",
    "creative_asset_id",
    "resource_item_id",
    "generation_id",
    "generation_task_id",
    "snapshot_id",
    "job_id",
    "task_id",
    "version_id",
    "clip_revision_id",
    "plot_revision_id",
    "profile_revision_id",
}
_TOOL_STATUS_FIELDS = {
    "status",
    "job_status",
    "processing_status",
    "generation_state",
    "lifecycle",
}
_SKIPPED_TOOL_FAMILIES = {
    "project.preference",
    "resource.kind",
    "ss_cli.document",
    "ss_cli.edit_lock",
}
logger = logging.getLogger(__name__)


def _flush_tracer_provider() -> None:
    try:
        from opentelemetry import trace as otel_trace

        provider = otel_trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()
    except Exception as exc:
        logger.warning("Failed to flush test tracer provider: %s", exc)


def _configured_path(env_name: str, default: Path) -> Path:
    return Path(os.environ.get(env_name, str(default))).expanduser().resolve()


def _configured_memory_types() -> set[str]:
    raw = os.environ.get("SS_MEMORY_TYPES", "")
    if not raw.strip():
        return set(MEMORY_TYPES)
    configured = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = configured - MEMORY_TYPES
    if unknown:
        raise ValueError(f"Unknown SS_MEMORY_TYPES: {sorted(unknown)}")
    if not configured:
        raise ValueError("SS_MEMORY_TYPES must enable at least one memory type")
    return configured


def _load_source(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    turns = payload.get("data", {}).get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError(f"SS session file has no data.turns: {path}")
    return payload


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _reference_index(payload: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Resolve resource IDs to stable Plot/Clip IDs from recorded tool results."""
    clip_parents: dict[str, str] = {}
    index: dict[str, list[dict[str, str]]] = {}

    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        clip_id = str(item.get("clip_id") or "")
        plot_id = str(item.get("plot_id") or "")
        if clip_id and plot_id:
            clip_parents[clip_id] = plot_id

    def record(resource_id: str, context: dict[str, Any]) -> None:
        clip_id = str(context.get("clip_id") or "")
        plot_id = str(context.get("plot_id") or clip_parents.get(clip_id) or "")
        normalized = {
            key: value
            for key, value in {
                "resource_id": resource_id,
                "plot_id": plot_id,
                "clip_id": clip_id,
                "take_id": str(context.get("take_id") or ""),
                "asset_id": str(context.get("creative_asset_id") or ""),
            }.items()
            if value
        }
        if len(normalized) <= 1:
            return
        bucket = index.setdefault(resource_id, [])
        if normalized not in bucket:
            bucket.append(normalized)

    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        resource_id = str(item.get("output_resource_asset_id") or "")
        if resource_id:
            record(resource_id, item)
        references = item.get("reference_resources")
        if isinstance(references, dict):
            for ref_id, ref_data in references.items():
                if isinstance(ref_data, dict) and isinstance(ref_data.get("owner_context"), dict):
                    record(str(ref_id), ref_data["owner_context"])
    return index


def _iter_recorded_tool_calls(turn: dict[str, Any]):
    """Yield each top-level or subagent tool call once, in recorded order."""
    seen: set[str] = set()

    def visit(value: Any):
        if isinstance(value, dict):
            call_id = value.get("callId")
            is_tool_call = (
                "tool" in value or bool(value.get("name")) or bool(_command_from_call(value))
            )
            if (
                call_id
                and is_tool_call
                and ("input" in value or "result" in value or "tool" in value)
            ):
                normalized_id = str(call_id)
                if normalized_id not in seen:
                    seen.add(normalized_id)
                    yield value
                return
            for child in value.values():
                yield from visit(child)
        elif isinstance(value, list):
            for child in value:
                yield from visit(child)

    yield from visit(turn.get("toolCalls") or [])
    yield from visit(turn.get("subagents") or [])


def _parse_json_result(text: Any) -> dict[str, Any] | None:
    """Parse the structured JSON portion of a recorded textual tool result."""
    if not isinstance(text, str) or not text:
        return None
    stdout = text.split("--- stdout ---", 1)[-1]
    stdout = stdout.split("--- stderr ---", 1)[0]
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(stdout[start:].lstrip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _command_from_call(call: dict[str, Any]) -> str:
    raw_input = call.get("input")
    if isinstance(raw_input, dict):
        return str(raw_input.get("command") or "")
    if not isinstance(raw_input, str):
        return ""
    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError:
        return raw_input
    return str(parsed.get("command") or "") if isinstance(parsed, dict) else ""


def _command_metadata(command: str) -> tuple[str, str, dict[str, str]]:
    match = _SS_COMMAND_RE.match(command)
    if not match:
        return "", "", {}
    resource = match.group("resource").replace("-", "_")
    verb = match.group("verb").replace("-", "_")
    family = f"{resource}.{verb}"
    flags = {
        item.group("key"): item.group("value").strip("\"'")
        for item in _SS_FLAG_RE.finditer(command)
    }
    return family, verb, flags


def _safe_status(value: Any) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    normalized = str(value)
    if len(normalized) > 40 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized):
        return None
    return normalized


def _compact_tool_record(
    source: Any,
    *,
    list_position: int | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}

    compact: dict[str, Any] = {}
    for source_key, target_key in _TOOL_FIELD_ALIASES.items():
        if source_key not in source or target_key in compact:
            continue
        value = source[source_key]
        if target_key in _TOOL_STATUS_FIELDS:
            value = _safe_status(value)
        elif not isinstance(value, str) or not value or len(value) > 128:
            value = None
        if value is not None and value != "":
            compact[target_key] = value

    has_identity = bool(_TOOL_IDENTITY_FIELDS & compact.keys())
    if not has_identity:
        return {}
    if list_position is not None and {"plot_id", "clip_id"} & compact.keys():
        compact["list_position"] = list_position
    return compact


def _walk_tool_records(value: Any, *, list_position: int | None = None):
    if isinstance(value, dict):
        compact = _compact_tool_record(value, list_position=list_position)
        if compact:
            yield compact
        for child in value.values():
            yield from _walk_tool_records(child, list_position=list_position)
    elif isinstance(value, list):
        for index, child in enumerate(value, start=1):
            yield from _walk_tool_records(child, list_position=index)


def _tool_result_payloads(call: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    tool = call.get("tool")
    if isinstance(tool, dict):
        output = tool.get("output")
        if isinstance(output, dict) and isinstance(output.get("raw_stdout_json"), dict):
            payloads.append(output["raw_stdout_json"])
        result = tool.get("result")
        if isinstance(result, dict):
            stdout_json = result.get("stdout_json")
            if isinstance(stdout_json, dict):
                payloads.append(stdout_json)
    parsed = _parse_json_result(call.get("result"))
    if parsed is not None:
        payloads.append(parsed)
    return payloads


def _tool_family_action_target(
    call: dict[str, Any],
) -> tuple[str, str, dict[str, Any], str]:
    tool = call.get("tool")
    if isinstance(tool, dict):
        family = str(tool.get("family") or "")
        action = str(tool.get("action") or "")
        target = _compact_tool_record(tool.get("target"))
        flags = tool.get("parameters", {}).get("flags")
        flag_record = _compact_tool_record(flags)
        target = {**flag_record, **target}
        return family, action, target, str(tool.get("command") or "")

    command = _command_from_call(call)
    family, action, flags = _command_metadata(command)
    return family, action, _compact_tool_record(flags), command


def _tool_family_is_relevant(family: str) -> bool:
    if not family:
        return False
    if any(
        family == prefix or family.startswith(prefix + ".") for prefix in _SKIPPED_TOOL_FAMILIES
    ):
        return False
    return family.split(".", 1)[0] in {
        "project",
        "plot",
        "clip",
        "asset",
        "resource",
        "generation",
        "ss_cli",
    }


def _tool_result_record_limit(family: str) -> int:
    if family in {"plot.list", "clip.list", "clip.takes"}:
        return 32
    if family in {"asset.list", "resource.list", "asset.item.list"}:
        return 4
    return 16


def _compact_tool_call(call: dict[str, Any], source_index: int) -> dict[str, Any] | None:
    family, action, target, _ = _tool_family_action_target(call)
    if not _tool_family_is_relevant(family):
        return None

    records: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    record_limit = _tool_result_record_limit(family)
    for payload in _tool_result_payloads(call):
        for record in _walk_tool_records(payload):
            key = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in seen_records:
                continue
            seen_records.add(key)
            records.append(record)
            if len(records) >= record_limit:
                break
        if len(records) >= record_limit:
            break

    identities = [target, *records]
    specific_fields = _TOOL_IDENTITY_FIELDS - {"project_id"}
    if not any(specific_fields & item.keys() for item in identities if item):
        return None

    execution: dict[str, Any] = {}
    phase = _safe_status(call.get("phase"))
    if phase:
        execution["phase"] = phase
    tool = call.get("tool")
    output = tool.get("output") if isinstance(tool, dict) else None
    if isinstance(output, dict):
        if isinstance(output.get("exit_code"), int):
            execution["exit_code"] = output["exit_code"]
        if isinstance(output.get("ok"), bool):
            execution["ok"] = output["ok"]
    for payload in _tool_result_payloads(call):
        if isinstance(payload.get("ok"), bool):
            execution["ok"] = payload["ok"]
            break
    if call.get("errorMessage"):
        execution["ok"] = False

    event: dict[str, Any] = {
        "family": family,
        "action": action,
        "execution": execution,
    }
    if target:
        event["target"] = target
    if records:
        event["objects"] = records
    event["_source_index"] = source_index
    return event


def _tool_event_priority(event: dict[str, Any]) -> tuple[int, int]:
    family = str(event.get("family") or "")
    target = event.get("target") or {}
    records = event.get("objects") or []
    combined = [target, *records]
    has_clip = any("clip_id" in item for item in combined)
    has_plot = any("plot_id" in item for item in combined)
    mutating = str(event.get("action") or "") in {
        "create",
        "add",
        "update",
        "patch",
        "generate",
        "submit_video",
        "submit_image",
    }
    if has_clip and mutating:
        priority = 0
    elif has_clip:
        priority = 1
    elif has_plot and (mutating or family == "plot.list"):
        priority = 2
    elif has_plot:
        priority = 3
    else:
        priority = 4
    return priority, int(event["_source_index"])


def _compact_tool_evidence(
    turn: dict[str, Any],
    *,
    max_chars: int = TOOL_EVIDENCE_MAX_CHARS,
) -> dict[str, Any]:
    """Project SS tool traces to bounded identity/status evidence.

    The projection deliberately drops prompts, command prose, result text,
    logs, media metadata, and model-authored descriptions. It is routing
    evidence only; schemas still require memory content to originate in the
    explicit user message.
    """
    calls = list(_iter_recorded_tool_calls(turn))
    events = [
        event
        for index, call in enumerate(calls)
        if (event := _compact_tool_call(call, index)) is not None
    ]
    events.sort(key=_tool_event_priority)

    def document(selected: list[dict[str, Any]], *, object_truncated: bool = False):
        cleaned = [
            {key: value for key, value in event.items() if key != "_source_index"}
            for event in selected
        ]
        return {
            "source_call_count": len(calls),
            "relevant_call_count": len(events),
            "included_call_count": len(cleaned),
            "truncated": len(cleaned) < len(events) or object_truncated,
            "calls": cleaned,
        }

    selected: list[dict[str, Any]] = []
    object_truncated = False
    for event in events:
        candidate = document([*selected, event], object_truncated=object_truncated)
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) <= max_chars:
            selected.append(event)
            continue

        objects = event.get("objects") or []
        if not objects:
            continue
        shortened = {**event, "objects": []}
        fitted_objects: list[dict[str, Any]] = []
        for record in objects:
            shortened["objects"] = [*fitted_objects, record]
            candidate = document([*selected, shortened], object_truncated=True)
            if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) > max_chars:
                shortened["objects"] = fitted_objects
                break
            fitted_objects.append(record)
        if fitted_objects:
            selected.append(shortened)
            object_truncated = object_truncated or len(fitted_objects) < len(objects)

    compact = document(selected, object_truncated=object_truncated)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        raise AssertionError(f"tool evidence exceeded {max_chars} characters: {len(encoded)}")
    return compact


def _iso_time(milliseconds: int | None) -> str:
    if not milliseconds:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def _messages_from_session(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build an extraction transcript with one leading SS identity context.

    Original user and assistant text is preserved in chronological order. Raw
    tool calls/results are omitted, matching the default user-memory provider.
    A single leading context contains only the stable identity mappings needed
    to route user-authored facts; it is never a source of memory prose.
    """
    turns = payload["data"]["turns"]
    project_id = str(payload.get("projectId") or "proj_00G6pawQ")
    references = _reference_index(payload)
    user_resource_ids = list(
        dict.fromkeys(
            resource_id
            for turn in turns
            for resource_id in RESOURCE_ID_RE.findall(str(turn.get("userMessage") or ""))
        )
    )
    resolved_references = {
        resource_id: references[resource_id]
        for resource_id in user_resource_ids
        if resource_id in references
    }
    turn_focus = [
        {
            "turn_label": f"T{turn_index:03d}",
            "turn_id": turn.get("turnId"),
            "recorded_current_focus": turn.get("currentFocus"),
        }
        for turn_index, turn in enumerate(turns, start=1)
        if turn.get("currentFocus")
    ]
    context = {
        "project_id": project_id,
        "turn_focus": turn_focus,
        "resolved_references": resolved_references,
        "rule": (
            "This block is system-supplied identity context, not user intent. "
            "Use it only to resolve stable IDs and object relationships. "
            "Memory prose must come only from explicit role=user text."
        ),
        "scope_routing": (
            "Write each fact at exactly one level. A concrete resolved clip reference "
            "routes that edit to ss_clip; one whole plot routes to ss_plot; a rule "
            "for multiple plots in this project routes to ss_project; ss_user requires "
            "explicit cross-project applicability and must be a no-op for project-only evidence."
        ),
    }
    encoded_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    first_timestamp = next(
        (turn.get("userMessageOccurredAt") for turn in turns if turn.get("userMessageOccurredAt")),
        None,
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "parts": [TextPart("[SS_CONTEXT BEGIN]\n" + encoded_context + "\n[SS_CONTEXT END]")],
            "created_at": _iso_time(first_timestamp),
            "turn_id": "ss-context",
        }
    ]
    tool_call_count = sum(len(list(_iter_recorded_tool_calls(turn))) for turn in turns)
    assistant_message_count = 0

    for turn_index, turn in enumerate(turns, start=1):
        user_text = str(turn.get("userMessage") or "").strip()
        if not user_text:
            continue
        created_at = _iso_time(turn.get("userMessageOccurredAt"))
        turn_id = str(turn.get("turnId") or f"ss-turn-{turn_index}")
        messages.append(
            {
                "role": "user",
                "parts": [TextPart(user_text)],
                "created_at": created_at,
                "turn_id": turn_id,
                "message_kind": "user_query",
            }
        )
        for assistant_index, assistant in enumerate(turn.get("agentMessages") or [], start=1):
            assistant_text = str(assistant.get("text") or "").strip()
            if not assistant_text:
                continue
            messages.append(
                {
                    "role": "assistant",
                    "parts": [TextPart(assistant_text)],
                    "created_at": _iso_time(assistant.get("occurredAt")),
                    "turn_id": turn_id,
                    "message_kind": "assistant_step",
                    "source_message_ids": [
                        str(assistant.get("id") or f"{turn_id}-assistant-{assistant_index}")
                    ],
                }
            )
            assistant_message_count += 1

    manifest = {
        "source_turn_count": len(turns),
        "projected_message_count": len(messages),
        "projected_user_messages": sum(item["role"] == "user" for item in messages),
        "projected_assistant_messages": assistant_message_count,
        "projected_system_messages": 1,
        "omitted_assistant_messages": 0,
        "source_tool_calls": tool_call_count,
        "included_tool_calls": 0,
        "identity_reference_count": len(resolved_references),
        "identity_context_chars": len(encoded_context),
        "context_augmentation": (
            "One leading SS_CONTEXT system message with stable project/focus/reference "
            "identity mappings; no raw tool calls or results"
        ),
        "content_source_policy": (
            "Only role=user text contributes memory content; system context resolves IDs; "
            "assistant text is context only"
        ),
    }
    return messages, manifest


def _load_test_config(workspace: Path, schema_dir: Path) -> dict[str, Any]:
    config_path = resolve_config_path(None, OPENVIKING_CONFIG_ENV, DEFAULT_OV_CONF)
    if config_path is None:
        raise FileNotFoundError("OpenViking ov.conf is required for the live extraction test")
    config = copy.deepcopy(load_json_config(config_path))
    config.setdefault("memory", {}).update(
        {
            "custom_templates_dir": str(schema_dir),
            "extraction_enabled": True,
            "session_skill_extraction_enabled": False,
            "link_enabled": False,
            "eager_prefetch": False,
        }
    )
    output_format = os.environ.get("SS_MEMORY_OUTPUT_FORMAT")
    if output_format:
        config["memory"]["extraction_output_format"] = output_format
    config.setdefault("agent_evolution", {})["enabled"] = False
    config.setdefault("storage", {})["workspace"] = str(workspace)
    return config


async def _wait_for_task(
    service: OpenVikingService,
    ctx: RequestContext,
    task_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await service.sessions.get_commit_task(task_id, ctx) or {}
        status = last.get("status")
        print(f"[ss-memory] task={task_id} status={status}", flush=True)
        if status in {"completed", "failed", "cancelled"}:
            return last
        await asyncio.sleep(1)
    raise TimeoutError(f"Session commit did not finish in {timeout}s: {last}")


def _uri_output_path(output_dir: Path, uri: str) -> Path:
    parsed = urlsplit(uri)
    relative = Path(parsed.netloc, *[part for part in parsed.path.split("/") if part])
    return output_dir / "files" / relative


def _expected_uris(manifest_path: Path, memory_types: set[str]) -> set[str]:
    if not manifest_path.exists():
        return set()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        str(item["uri"])
        for item in manifest.get("schema_instances", [])
        if item.get("uri") and item.get("memory_ids") and f"ss_{item.get('scope')}" in memory_types
    }


async def run_actual_extraction(
    *,
    source_path: Path,
    expected_manifest_path: Path,
    schema_dir: Path,
    output_dir: Path,
    workspace: Path,
    timeout: float,
) -> dict[str, Any]:
    payload = _load_source(source_path)
    message_specs, input_manifest = _messages_from_session(payload)
    enabled_memory_types = _configured_memory_types()
    input_manifest["enabled_memory_types"] = sorted(enabled_memory_types)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "input_manifest.json").write_text(
        json.dumps(input_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # These caches are process-global. Reset them so the test always loads the
    # schemas and model/storage handles from this isolated configuration.
    from openviking.session.memory import memory_type_registry as registry_module
    from openviking.session.memory import streaming_memory_updater as updater_module

    OpenVikingConfigSingleton.reset_instance()
    registry_module._default_registry = None
    updater_module._streaming_memory_updater_registry.clear()
    OpenVikingConfigSingleton.initialize(
        config_dict=_load_test_config(workspace=workspace, schema_dir=schema_dir)
    )

    user = UserIdentifier("default", "ss_memory_eval_user")
    ctx = RequestContext(user=user, role=Role.USER)
    service: OpenVikingService | None = None
    try:
        service = OpenVikingService(path=str(workspace), user=user)
        await service.initialize()
        session_id = f"ss-ground-truth-{int(time.time())}"
        memory_policy = {
            "self": {"enabled": True},
            "peer": {"enabled": False},
            "working_memory": {"enabled": False},
            "memory_types": sorted(enabled_memory_types),
        }
        auto_commit_policy = {
            "pending_token_threshold": 55_000,
            "message_count_threshold": 200,
            "idle_timeout_seconds": 86_400,
            "keep_recent_count": 0,
            "min_commit_interval_seconds": 0,
        }
        session = await service.sessions.create(
            ctx,
            session_id=session_id,
            memory_policy=memory_policy,
            auto_commit_policy=auto_commit_policy,
        )
        await session.add_messages_async(message_specs)
        commit = await session.commit_async(memory_policy=memory_policy)
        task_id = str(commit.get("task_id") or "")
        if not task_id:
            raise AssertionError(f"Session commit did not return a task_id: {commit}")
        task = await _wait_for_task(service, ctx, task_id, timeout)
        if task.get("status") != "completed":
            raise AssertionError(f"Session commit failed: {task}")

        archive_uri = str(commit.get("archive_uri") or "")
        diff_uri = f"{archive_uri.rstrip('/')}/memory_diff.json"
        memory_diff = json.loads(await service.fs.read(diff_uri, ctx=ctx))
        operations = memory_diff.get("operations") or {}
        changed = [
            item
            for kind in ("adds", "updates")
            for item in (operations.get(kind) or [])
            if item.get("memory_type") in enabled_memory_types and item.get("uri")
        ]
        # A batched commit may update the same URI several times. Preserve the
        # operation history in memory_diff, but materialize each final file once.
        latest_change_by_uri = {str(item["uri"]): item for item in changed}
        actual_files = []
        for item in latest_change_by_uri.values():
            uri = str(item["uri"])
            raw = await service.fs.read(uri, ctx=ctx)
            destination = _uri_output_path(output_dir, uri)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(raw, encoding="utf-8")
            parsed = MemoryFileUtils.read(raw, uri=uri)
            actual_files.append(
                {
                    "memory_type": item["memory_type"],
                    "uri": uri,
                    "path": str(destination.relative_to(output_dir)),
                    "content": parsed.content,
                    "fields": parsed.extra_fields,
                }
            )

        actual_uris = {item["uri"] for item in actual_files}
        expected_uris = _expected_uris(expected_manifest_path, enabled_memory_types)
        intersection = actual_uris & expected_uris
        comparison = {
            "expected_uri_count": len(expected_uris),
            "actual_uri_count": len(actual_uris),
            "matched_uri_count": len(intersection),
            "uri_precision": len(intersection) / len(actual_uris) if actual_uris else 0.0,
            "uri_recall": len(intersection) / len(expected_uris) if expected_uris else None,
            "matched_uris": sorted(intersection),
            "missing_expected_uris": sorted(expected_uris - actual_uris),
            "unexpected_actual_uris": sorted(actual_uris - expected_uris),
            "user_memory_created": any(item["memory_type"] == "ss_user" for item in actual_files),
            "note": "URI comparison measures object routing only; semantic field correctness requires reviewing actual files against ground_truth.json.",
        }
        result = {
            "source": str(source_path),
            "schemas": str(schema_dir),
            "enabled_memory_types": sorted(enabled_memory_types),
            "session_id": session_id,
            "commit": commit,
            "task": task,
            "archive_uri": archive_uri,
            "memory_diff": memory_diff,
            "actual_files": actual_files,
            "comparison": comparison,
        }
        (output_dir / "actual_extraction.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(comparison, ensure_ascii=False, indent=2), flush=True)
        return result
    finally:
        if service is not None:
            await service.close()
        updater_module._streaming_memory_updater_registry.clear()
        registry_module._default_registry = None
        OpenVikingConfigSingleton.reset_instance()


def test_narrow_scope_schemas_require_substantive_user_facts() -> None:
    user_schema = (FIXTURE_SCHEMAS / "ss_user.yaml").read_text(encoding="utf-8")
    plot_schema = (FIXTURE_SCHEMAS / "ss_plot.yaml").read_text(encoding="utf-8")
    clip_schema = (FIXTURE_SCHEMAS / "ss_clip.yaml").read_text(encoding="utf-8")
    project_schema = (FIXTURE_SCHEMAS / "ss_project.yaml").read_text(encoding="utf-8")

    assert "禁止用空字符串、空 blocks" in user_schema
    assert "创建门槛（必须满足）" in plot_schema
    assert "否则必须 no-op" in plot_schema
    assert "不得按工具列出的 plot_id 批量下沉" in plot_schema
    assert "没有此类原话时必须 no-op" in clip_schema
    assert "不能复制为记忆正文" in clip_schema
    assert "不得因为全量抽取同时看到了某个 Plot" in project_schema
    assert "不能为每个 Plot/Clip" in project_schema
    assert "ss_project 是同 project_id 下所有 ss_plot 的父级" in project_schema
    assert "每个 ss_plot 只属于一个 ss_project" in plot_schema
    assert "每个 ss_clip 只属于一个父 ss_plot" in clip_schema
    assert (
        'filename_template: "{{ project_id }}/plots/{{ plot_id }}/clips/{{ clip_id }}/clip.md"'
        in clip_schema
    )


def test_configured_memory_types_supports_isolated_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_MEMORY_TYPES", "ss_plot")
    assert _configured_memory_types() == {"ss_plot"}

    monkeypatch.setenv("SS_MEMORY_TYPES", "ss_plot,unknown")
    with pytest.raises(ValueError, match="Unknown SS_MEMORY_TYPES"):
        _configured_memory_types()


def test_expected_uris_filters_manifest_scope(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_instances": [
                    {"scope": "project", "uri": "viking://project", "memory_ids": ["P01"]},
                    {"scope": "plot", "uri": "viking://plot", "memory_ids": ["S01"]},
                    {"scope": "clip", "uri": "viking://clip", "memory_ids": []},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert _expected_uris(manifest, {"ss_plot"}) == {"viking://plot"}


def test_compact_tool_evidence_is_bounded_and_drops_tool_prose() -> None:
    secret_prompt = "TOOL_PROMPT_MUST_NOT_SURVIVE"
    secret_result_text = "TOOL_RESULT_PROSE_MUST_NOT_SURVIVE"
    items = [
        {
            "clip_id": f"clip_{index:03d}",
            "plot_id": "plot_scene01",
            "duration_sec": 10,
            "title": f"片段 {index}",
            "text": secret_result_text,
        }
        for index in range(40)
    ]
    result = json.dumps({"ok": True, "data": {"items": items}}, ensure_ascii=False)
    turn = {
        "toolCalls": [
            {
                "callId": "call-segment-list",
                "phase": "completed",
                "input": json.dumps(
                    {
                        "command": (
                            f'ss-cli clip list -plot-id plot_scene01 -prompt "{secret_prompt}"'
                        ),
                        "description": secret_prompt,
                    },
                    ensure_ascii=False,
                ),
                "result": f"exit_code: 0\n--- stdout ---\n{result}\n\n--- stderr ---\n",
            }
        ]
    }

    compact = _compact_tool_evidence(turn, max_chars=700)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    assert len(encoded) <= 700
    assert compact["truncated"] is True
    assert compact["included_call_count"] == 1
    assert "plot_scene01" in encoded
    assert "clip_000" in encoded
    assert "identity_title" not in encoded
    assert "duration_sec" not in encoded
    assert secret_prompt not in encoded
    assert secret_result_text not in encoded


def test_messages_put_one_identity_context_first_and_omit_tools() -> None:
    payload = {
        "projectId": "proj_test",
        "data": {
            "turns": [
                {
                    "turnId": "turn-1",
                    "userMessage": ('<reference source="video|ra_test">把这个片段调整为十秒。'),
                    "userMessageOccurredAt": 1_700_000_000_000,
                    "currentFocus": {"id": "clip_old", "type": "clip"},
                    "agentMessages": [{"text": "AGENT_PROSE_MUST_NOT_SURVIVE"}],
                    "toolCalls": [],
                    "subagents": [
                        {
                            "toolCalls": [
                                {
                                    "callId": "call-patch",
                                    "phase": "completed",
                                    "tool": {
                                        "output": {
                                            "raw_stdout_json": {
                                                "data": {
                                                    "reference_resources": {
                                                        "ra_test": {
                                                            "owner_context": {
                                                                "clip_id": "clip_actual",
                                                                "plot_id": "plot_actual",
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    },
                                    "input": json.dumps(
                                        {
                                            "command": (
                                                "ss-cli clip patch -clip-id clip_actual "
                                                "-duration-sec 10 -text TOOL_TEXT_MUST_NOT_SURVIVE"
                                            )
                                        }
                                    ),
                                    "result": (
                                        "exit_code: 0\n--- stdout ---\n"
                                        '{"ok":true,"data":{"clip_id":"clip_actual",'
                                        '"plot_id":"plot_actual","duration_sec":10,'
                                        '"text":"TOOL_RESULT_MUST_NOT_SURVIVE"}}'
                                        "\n\n--- stderr ---\n"
                                    ),
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    }

    messages, manifest = _messages_from_session(payload)
    context_text = messages[0]["parts"][0].text
    all_text = "\n".join(message["parts"][0].text for message in messages)

    assert [message["role"] for message in messages] == ["system", "user", "assistant"]
    assert context_text.startswith("[SS_CONTEXT BEGIN]\n")
    assert context_text.endswith("\n[SS_CONTEXT END]")
    assert "clip_actual" in context_text
    assert "plot_actual" in context_text
    assert messages[1]["parts"][0].text == (
        '<reference source="video|ra_test">把这个片段调整为十秒。'
    )
    assert messages[2]["parts"][0].text == "AGENT_PROSE_MUST_NOT_SURVIVE"
    assert sum("[SS_CONTEXT BEGIN]" in message["parts"][0].text for message in messages) == 1
    assert "TOOL_TEXT_MUST_NOT_SURVIVE" not in all_text
    assert "TOOL_RESULT_MUST_NOT_SURVIVE" not in all_text
    assert manifest["source_tool_calls"] == 1
    assert manifest["included_tool_calls"] == 0
    assert manifest["identity_reference_count"] == 1
    assert manifest["projected_system_messages"] == 1
    assert manifest["projected_assistant_messages"] == 1


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_SS_MEMORY_EXTRACTION_TEST") != "1",
    reason="set RUN_SS_MEMORY_EXTRACTION_TEST=1 to run the live SS memory extraction",
)
def test_real_ss_session_extracts_four_level_memory(tmp_path: Path) -> None:
    source = _configured_path("SS_MEMORY_SESSION_FILE", DEFAULT_SOURCE)
    expected = _configured_path("SS_MEMORY_EXPECTED_MANIFEST", DEFAULT_EXPECTED)
    output_base = _configured_path("SS_MEMORY_ACTUAL_DIR", tmp_path / "actual")
    run_id = os.environ.get("SS_MEMORY_RUN_ID") or datetime.now().strftime("run-%Y%m%d-%H%M%S")
    output = output_base / run_id
    initialized = init_tracer_from_server_config(load_server_config())
    if initialized is None or not tracer.is_enabled():
        pytest.fail("failed to initialize tracer; check server.observability.traces")
    try:
        with tracer.start_as_current_span("tests.integration.ss_four_level_memory_extraction"):
            trace_id = tracer.get_trace_id()
            print(f"[ss-memory] trace_id={trace_id}", flush=True)
            result = asyncio.run(
                run_actual_extraction(
                    source_path=source,
                    expected_manifest_path=expected,
                    schema_dir=FIXTURE_SCHEMAS.resolve(),
                    output_dir=output,
                    workspace=tmp_path / "workspace",
                    timeout=float(os.environ.get("SS_MEMORY_TIMEOUT", "1800")),
                )
            )
            (output / "trace_id.txt").write_text(trace_id + "\n", encoding="utf-8")
    finally:
        _flush_tracer_provider()
    actual_files = result["actual_files"]
    assert actual_files, "the real extraction pipeline produced no SS memory files"
    enabled_memory_types = set(result["enabled_memory_types"])
    assert {item["memory_type"] for item in actual_files} <= enabled_memory_types
    for memory_type in enabled_memory_types - {"ss_user"}:
        assert any(item["memory_type"] == memory_type for item in actual_files)
    if "ss_user" in enabled_memory_types:
        assert not result["comparison"]["user_memory_created"]
