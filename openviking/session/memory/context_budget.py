# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Input-budget helpers for memory extraction.

Memory files can grow far beyond a model's input limit.  Reads therefore keep
the authoritative ``MemoryFile`` in process while exposing only a bounded view
to the model.  ``partial_read_fields`` records exactly which original snippets
were visible so later writes can be restricted to local, exact patches.
"""

from __future__ import annotations

import json
import re
from typing import Any

from openviking.utils.token_estimation import estimate_text_tokens


class MemoryInputBudgetError(ValueError):
    """Raised before a model request that exceeds the configured input budget."""


def json_tokens(value: Any) -> int:
    """Conservatively estimate tokens for a JSON request payload."""
    return estimate_text_tokens(json.dumps(value, ensure_ascii=False, default=str))


def _text_prefix(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _visible_text(
    text: str,
    budget: int,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = -1,
    line_numbers: bool = False,
) -> tuple[str, list[str], bool, int | None]:
    """Return a bounded set of exact line spans from ``text``."""
    lines = text.splitlines(keepends=True)
    stop = len(lines) if limit < 0 else min(len(lines), offset + limit)
    indices = list(range(min(offset, len(lines)), stop))
    if not indices:
        return "", [], bool(text), None

    if query:
        terms = set(re.findall(r"[a-zA-Z0-9_]{3,}|[\u4e00-\u9fff]{2}", query.lower()))
        scores = {index: sum(term in lines[index].lower() for term in terms) for index in indices}
        # Preserve the opening shape, then prefer relevant and recent lines.
        order = list(
            dict.fromkeys(
                indices[:3] + sorted(indices, key=lambda index: (-scores[index], -index))
            )
        )
    else:
        order = indices

    selected: dict[int, str] = {}
    remaining = max(0, budget)
    for index in order:
        if remaining < 40:
            break
        line = lines[index]
        cost = estimate_text_tokens(line) + 12
        if cost <= remaining:
            selected[index] = line
            remaining -= cost
        elif not selected or (index < offset + 3 and remaining > 80):
            piece = _text_prefix(line, max(0, remaining - 16))
            if piece:
                selected[index] = piece
                remaining -= estimate_text_tokens(piece) + 12

    rendered: list[str] = []
    spans: list[str] = []
    current = ""
    previous: int | None = None
    for index in sorted(selected):
        piece = selected[index]
        contiguous = (
            previous is not None
            and index == previous + 1
            and selected[previous] == lines[previous]
        )
        if previous is not None and not contiguous:
            if current:
                spans.append(current)
                current = ""
            rendered.append("[... omitted; use read offset/limit or field/text_offset ...]")
        current += piece
        visible_line = piece.rstrip("\r\n")
        rendered.append(f"{index + 1}\t{visible_line}" if line_numbers else visible_line)
        previous = index
    if current:
        spans.append(current)

    complete = (
        len(selected) == len(lines)
        and all(selected.get(index) == line for index, line in enumerate(lines))
    )
    next_offset = next((index for index in indices if index not in selected), None)
    return "\n".join(rendered), spans, not complete, next_offset


def memory_read_view(
    memory_file: Any,
    budget: int,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = -1,
    field: str | None = None,
    text_offset: int = 0,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Build an LLM-visible read result and its partial-field constraints."""
    metadata = memory_file.to_metadata()
    for key in (
        "links",
        "backlinks",
        "content",
        "source_extraction_id",
        "source_extraction_ids",
        "last_update_trace_id",
    ):
        metadata.pop(key, None)
    plain_content = memory_file.plain_content() or ""
    values = {**metadata, "content": plain_content}

    if field is not None:
        if field not in values or not isinstance(values[field], str):
            raise ValueError("Field read requires an existing string field")
        value = values[field]
        result: dict[str, Any] = {
            "_read_uri": memory_file.uri,
            "_field": field,
            "_text_offset": text_offset,
            "_read_rule": (
                "Patch this exact field value. Omitted fields require local exact patches; "
                "never replace or delete unread content."
            ),
        }
        constraints = {name: [] for name in values}
        available = max(0, budget - json_tokens(result) - json_tokens(list(constraints)) - 160)
        piece = _text_prefix(value[text_offset:], available)
        result[field] = piece
        while json_tokens({**result, "_omitted_fields": list(constraints)}) > budget and piece:
            piece = piece[: len(piece) * 9 // 10]
            result[field] = piece
        if text_offset == 0 and piece == value:
            constraints.pop(field, None)
        else:
            constraints[field] = [piece] if piece else []
            if text_offset + len(piece) < len(value):
                result["_next_text_offset"] = text_offset + len(piece)
        result["_partial"] = bool(constraints)
        if constraints:
            result["_omitted_fields"] = list(constraints)
        return result, constraints

    complete = dict(metadata)
    complete["content"] = "\n".join(
        f"{index + 1}\t{line}" for index, line in enumerate(plain_content.splitlines())
    )
    complete["_read_uri"] = memory_file.uri
    complete["_partial"] = False
    if offset == 0 and limit == -1 and json_tokens(complete) <= budget:
        return complete, {}

    result = {}
    constraints: dict[str, list[str]] = {}
    remaining = max(512, budget) - 192
    for key, value in metadata.items():
        cost = json_tokens({key: value})
        if cost <= min(256, max(0, remaining // 5)):
            result[key] = value
            remaining -= cost
        elif isinstance(value, str):
            visible, spans, partial, _next = _visible_text(
                value,
                min(256, max(64, remaining // 6)),
                query=query,
            )
            result[key] = visible
            if partial:
                constraints[key] = spans
            remaining -= json_tokens({key: visible})
        else:
            constraints[key] = []

    body, spans, partial, next_offset = _visible_text(
        plain_content,
        max(128, remaining - 96),
        query=query,
        offset=offset,
        limit=limit,
        line_numbers=True,
    )
    result["content"] = body
    if partial:
        constraints["content"] = spans
    result["_read_uri"] = memory_file.uri
    result["_partial"] = bool(constraints)
    if constraints:
        result["_omitted_fields"] = list(constraints)
        result["_read_rule"] = (
            "Only exact unique edit/drop operations inside visible original text are allowed. "
            "Do not replace a partial field or delete the file."
        )
    if next_offset is not None:
        result["_next_offset"] = next_offset
    return result, constraints


def apply_exact_string_patch(current: Any, patch: Any, spans: list[str] | None = None) -> str:
    """Apply a string patch only when every search is exact and unique."""
    from openviking.session.memory.merge_op.patch_handler import unescape_markers

    blocks = patch.get("blocks", []) if isinstance(patch, dict) else patch.blocks
    for block in blocks:
        search = unescape_markers(
            block.get("search", block.get("delete", ""))
            if isinstance(block, dict)
            else block.search
        )
        replacement = unescape_markers(
            block.get("replace", "") if isinstance(block, dict) else block.replace
        )
        if current in (None, "") and not search and len(blocks) == 1 and spans is None:
            return replacement
        if not isinstance(current, str) or not search or current.count(search) != 1:
            raise ValueError("Every SEARCH block must match exactly once in its original field")
        if spans is not None and not any(search in span for span in spans):
            raise ValueError("SEARCH must match visible original field text")
        current = current.replace(search, replacement, 1)
    if not isinstance(current, str):
        raise ValueError("String patch requires a string field")
    return current


def memory_file_field_value(memory_file: Any, name: str) -> Any:
    """Read a MemoryFile field from the same location used by serialization."""
    if name == "content":
        return memory_file.plain_content()
    if name == "memory_type":
        return memory_file.memory_type
    return memory_file.extra_fields.get(name)


def validate_partial_fields(
    memory_file: Any, fields: dict[str, Any], constraints: dict[str, list[str]]
) -> None:
    """Reject full or out-of-view writes to partially read fields."""
    from openviking.session.memory.merge_op.base import StrPatch

    for name, spans in constraints.items():
        if name not in fields:
            continue
        current = memory_file_field_value(memory_file, name)
        patch = fields[name]
        if patch == current:
            continue
        blocks = (
            patch.blocks
            if isinstance(patch, StrPatch)
            else patch.get("blocks")
            if isinstance(patch, dict)
            else None
        )
        if blocks == []:
            continue
        if blocks is None or not isinstance(current, str):
            raise ValueError(
                f"Partial read requires a local string patch: {memory_file.uri} field={name}"
            )
        try:
            apply_exact_string_patch(current, patch, spans)
        except ValueError as exc:
            raise ValueError(
                f"Patch must uniquely match visible original text: {memory_file.uri} field={name}"
            ) from exc
