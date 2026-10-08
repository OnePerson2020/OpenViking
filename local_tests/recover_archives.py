#!/usr/bin/env python3
"""Audit and serially recover OpenViking Session archives.

The default mode is read-only and writes a fresh, hash-bound inventory.  The
execution mode accepts only that inventory, revalidates every gate immediately
before submission, and stops on the first ambiguous write or unsafe failure.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path("/data00/home/mayunxiang.26/.openviking")
BASE = ROOT / "local_patches/remediation-20260915"
DATA = ROOT / "data/viking/default"
QUEUE_DB = ROOT / "data/_system/queue/queue.db"
CONFIG_PATH = ROOT / "ov.conf"
RESULTS_PATH = BASE / "recovery-results.jsonl"
DEFAULT_INVENTORY = BASE / "recovery-inventory-current.json"
ACTIVE_TASK_STATES = {"pending", "running", "cancelling"}
TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}
SAFE_FAILURE_TERMS = (
    "maximum context length",
    "context length exceeded",
    "context window",
    "prompt is too long",
    "input is too long",
    "timeout",
    "timed out",
    "overload",
    "rate limit",
    "too many requests",
    "429",
    # Compatibility defect fixed by the 2026-09-15 VLMConfig forwarding patch.
    # Keep this exact so unrelated TypeError failures still require review.
    "vlmconfig.get_completion_async() got an unexpected keyword argument 'max_tokens'",
)
MANUAL_FAILURE_TERMS = (
    "image",
    "obj.field.edit",
    "must occur exactly once",
    "read uri",
    "before write",
)
UNSAFE_ERROR_TERMS = (
    "patch",
    "budget",
    "input tokens",
    "before write",
    "requires review",
    "read uri",
    "peer scope",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )


def request(method: str, path: str, key: str, body: Any = None) -> dict[str, Any]:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "http://127.0.0.1:1933" + path,
        data=payload,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=45) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("OpenViking API returned a non-object response")
    return result


def decode_queue_payload(raw: Any) -> dict[str, Any]:
    """Decode QueueFS's SQLite envelope, including byte-integer arrays."""
    value = raw
    for _ in range(4):
        if isinstance(value, bytes):
            value = value.decode("utf-8")
            continue
        if isinstance(value, str):
            value = json.loads(value)
            continue
        if isinstance(value, list) and all(
            isinstance(item, int) and 0 <= item <= 255 for item in value
        ):
            value = bytes(value).decode("utf-8")
            continue
        if isinstance(value, dict) and "data" in value:
            value = value["data"]
            continue
        break
    return value if isinstance(value, dict) else {}


def load_live_queue() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    with sqlite3.connect(f"file:{QUEUE_DB}?mode=ro", uri=True) as db:
        query = (
            "SELECT id, message_id, data, status, processing_started_at, created_at "
            "FROM queue_messages WHERE queue_name = ? ORDER BY id"
        )
        for row_id, message_id, raw, status, processing_at, created_at in db.execute(
            query, ("SessionCommit",)
        ):
            counts[str(status)] += 1
            payload = decode_queue_payload(raw)
            rows.append(
                {
                    "row_id": row_id,
                    "message_id": message_id,
                    "status": status,
                    "processing_started_at": processing_at,
                    "created_at": created_at,
                    "task_id": payload.get("task_id"),
                    "session_id": payload.get("session_id"),
                    "session_uri": payload.get("session_uri"),
                    "archive_uri": payload.get("archive_uri"),
                    "user": payload.get("user"),
                }
            )
    return rows, {"counts": dict(sorted(counts.items())), "total": len(rows)}


def load_tasks() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    malformed: list[str] = []
    task_root = DATA / "_system/tasks"
    for path in task_root.glob("*/*.json"):
        try:
            task = read_json(path)
        except Exception:
            malformed.append(str(path))
            continue
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            malformed.append(str(path))
            continue
        task["_path"] = str(path)
        tasks[task_id] = task
        counts[str(task.get("status") or "unknown")] += 1
    return tasks, {
        "counts": dict(sorted(counts.items())),
        "total": len(tasks),
        "malformed": malformed,
    }


def archive_index(name: str) -> int:
    prefix, separator, suffix = name.rpartition("_")
    if prefix != "archive" or not separator or not suffix.isdigit():
        raise ValueError(f"Invalid archive name: {name}")
    return int(suffix)


def archive_message_ids(path: Path) -> list[str]:
    result: list[str] = []
    for line_number, line in enumerate(
        (path / "messages.jsonl").read_text().replace("\r\n", "\n").split("\n"),
        start=1,
    ):
        if not line.strip():
            continue
        item = json.loads(line)
        message_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise ValueError(f"missing message id at line {line_number}")
        result.append(message_id)
    if not result:
        raise ValueError("archive has no messages")
    return result


def completed_steps(meta: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = defaultdict(set)
    raw = meta.get("completed_memory_steps")
    if isinstance(raw, dict):
        for step, message_ids in raw.items():
            if isinstance(step, str) and isinstance(message_ids, list):
                merged[step].update(item for item in message_ids if isinstance(item, str))
    for receipt in receipts:
        message_ids = receipt.get("message_ids")
        if isinstance(message_ids, list):
            merged["long_term"].update(
                item for item in message_ids if isinstance(item, str)
            )
    return {step: sorted(values) for step, values in sorted(merged.items())}


def done_coverage(history: Path) -> set[str]:
    covered: set[str] = set()
    archives: dict[int, str] = {}
    for path in history.glob("archive_*"):
        if not path.is_dir():
            continue
        try:
            archives[archive_index(path.name)] = path.name
        except ValueError:
            continue
    for done_path in history.glob("archive_*/.done"):
        try:
            marker = read_json(done_path)
            current = archive_index(done_path.parent.name)
        except Exception:
            continue
        explicit = marker.get("covered_failed_archives")
        if isinstance(explicit, list):
            covered.update(item for item in explicit if isinstance(item, str))
        try:
            start = archive_index(str(marker.get("coverage_start_archive") or done_path.parent.name))
            end = archive_index(str(marker.get("coverage_end_archive") or done_path.parent.name))
        except ValueError:
            continue
        low = max(1, min(start, end, current))
        high = min(current, max(start, end))
        covered.update(name for index, name in archives.items() if low <= index <= high)
    return covered


def audit_archive(
    path: Path,
    *,
    queue_by_archive: dict[str, list[dict[str, Any]]],
    queue_by_session: dict[str, list[dict[str, Any]]],
    tasks: dict[str, dict[str, Any]],
    covered: set[str],
) -> dict[str, Any]:
    relative = path.relative_to(DATA / "user")
    user, sessions_literal, session, history_literal, archive = relative.parts
    if sessions_literal != "sessions" or history_literal != "history":
        raise ValueError(f"Unexpected archive path: {path}")
    session_uri = f"viking://user/{user}/sessions/{session}"
    archive_uri = f"{session_uri}/history/{archive}"
    result: dict[str, Any] = {
        "user": user,
        "session": session,
        "archive": archive,
        "path": str(path),
        "session_uri": session_uri,
        "archive_uri": archive_uri,
        "bytes": 0,
        "eligible": False,
        "reason": "unknown",
    }
    done_path = path / ".done"
    failed_path = path / ".failed.json"
    meta_path = path / ".meta.json"
    messages_path = path / "messages.jsonl"
    result["state"] = (
        "completed" if done_path.exists() else "failed" if failed_path.exists() else "pending"
    )
    if archive in covered:
        result["reason"] = "already_covered"
        return result
    if result["state"] == "completed":
        result["reason"] = "already_completed"
        return result
    if not meta_path.is_file() or not messages_path.is_file():
        result["reason"] = "missing_meta_or_messages"
        return result
    try:
        meta = read_json(meta_path)
        message_ids = archive_message_ids(path)
    except Exception as exc:
        result["reason"] = f"invalid_archive:{type(exc).__name__}:{exc}"
        return result
    phase1 = meta.get("phase1")
    queue_snapshot = phase1.get("queue_message") if isinstance(phase1, dict) else None
    if not isinstance(phase1, dict) or phase1.get("status") != "ready":
        result["reason"] = "phase1_not_ready"
        return result
    if not isinstance(queue_snapshot, dict):
        result["reason"] = "phase1_queue_message_missing"
        return result
    task_id = str(queue_snapshot.get("task_id") or "")
    if not task_id:
        result["reason"] = "phase1_task_id_missing"
        return result
    result["task_id"] = task_id
    result["task_status"] = (tasks.get(task_id) or {}).get("status")
    result["bytes"] = messages_path.stat().st_size
    result["messages_sha256"] = hashlib.sha256(messages_path.read_bytes()).hexdigest()
    result["message_count"] = len(message_ids)

    if queue_by_archive.get(archive_uri):
        result["reason"] = "archive_has_queue_owner"
        return result
    if queue_by_session.get(session_uri):
        result["reason"] = "session_has_queue_owner"
        return result
    task = tasks.get(task_id)
    if task and task.get("status") == "cancelled":
        result["reason"] = "original_task_cancelled"
        return result

    receipts: list[dict[str, Any]] = []
    receipt_states: Counter[str] = Counter()
    for receipt_path in sorted(path.glob(".long-term-*.json")):
        try:
            receipt = read_json(receipt_path)
        except Exception:
            result["reason"] = "unreadable_long_term_receipt"
            return result
        status = str(receipt.get("status") or "unknown").lower()
        receipt_states[status] += 1
        if status != "done":
            result["reason"] = f"ambiguous_long_term_receipt:{status}"
            return result
        message_id_values = receipt.get("message_ids")
        if not isinstance(message_id_values, list) or not all(
            isinstance(item, str) for item in message_id_values
        ):
            result["reason"] = "invalid_completed_receipt"
            return result
        receipts.append(receipt)
    progress = completed_steps(meta, receipts)
    recorded_ids = {item for values in progress.values() for item in values}
    if not recorded_ids.issubset(set(message_ids)):
        result["reason"] = "progress_references_foreign_messages"
        return result
    result["receipt_states"] = dict(sorted(receipt_states.items()))
    result["completed_memory_steps"] = progress
    result["memory_diff_exists"] = (path / "memory_diff.json").is_file()
    if result["memory_diff_exists"] and not progress.get("long_term"):
        result["reason"] = "memory_diff_without_completion_progress"
        return result

    if result["state"] == "failed":
        try:
            failure = read_json(failed_path)
        except Exception as exc:
            result["reason"] = f"invalid_failure_marker:{type(exc).__name__}"
            return result
        result["failure_stage"] = failure.get("stage")
        result["failure_error"] = str(failure.get("error") or "")[:1000]
        failure_text = f"{failure.get('stage', '')} {failure.get('error', '')}".lower()
        if "cancel" in failure_text:
            result["reason"] = "cancelled_archive"
            return result
        if any(term in failure_text for term in MANUAL_FAILURE_TERMS):
            result["reason"] = "failure_requires_manual_review"
            return result
        if not any(term in failure_text for term in SAFE_FAILURE_TERMS):
            result["reason"] = "failure_requires_manual_review"
            return result
        result["recovery_kind"] = "failed"
    else:
        result["recovery_kind"] = "ownerless_ready"
        if task and task.get("status") in ACTIVE_TASK_STATES:
            result["stale_task_status"] = task.get("status")

    result["eligible"] = True
    result["reason"] = "safe_candidate"
    return result


def generate_inventory(output: Path) -> dict[str, Any]:
    queue_rows, queue_summary = load_live_queue()
    tasks, task_summary = load_tasks()
    queue_by_archive: dict[str, list[dict[str, Any]]] = defaultdict(list)
    queue_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in queue_rows:
        if item.get("archive_uri"):
            queue_by_archive[str(item["archive_uri"])].append(item)
        if item.get("session_uri"):
            queue_by_session[str(item["session_uri"])].append(item)

    records: list[dict[str, Any]] = []
    archive_paths = sorted(
        DATA.glob("user/*/sessions/*/history/archive_*"), key=lambda path: str(path)
    )
    for history, paths in _group_by_history(archive_paths).items():
        covered = done_coverage(history)
        for path in paths:
            records.append(
                audit_archive(
                    path,
                    queue_by_archive=queue_by_archive,
                    queue_by_session=queue_by_session,
                    tasks=tasks,
                    covered=covered,
                )
            )

    # Keep the explicitly requested canary visible even if migration removed it
    # from the live user tree.  The script never restores it from backup.
    priority_key = ("vikingbot", "__openviking_resource_reason__", "archive_138")
    if not any(
        (item.get("user"), item.get("session"), item.get("archive")) == priority_key
        for item in records
    ):
        records.append(
            {
                "user": priority_key[0],
                "session": priority_key[1],
                "archive": priority_key[2],
                "path": str(
                    DATA
                    / "user/vikingbot/sessions/__openviking_resource_reason__/history/archive_138"
                ),
                "eligible": False,
                "reason": "missing_live_archive",
            }
        )

    records.sort(
        key=lambda item: (
            0
            if (item.get("user"), item.get("session"), item.get("archive"))
            == priority_key
            else 1,
            0 if item.get("recovery_kind") == "failed" else 1,
            int(item.get("bytes") or 0),
            str(item.get("user") or ""),
            str(item.get("session") or ""),
            str(item.get("archive") or ""),
        )
    )
    reasons = Counter(str(item.get("reason") or "unknown") for item in records)
    inventory = {
        "version": 2,
        "generated_at": utc_now(),
        "data_root": str(DATA),
        "queue_db": str(QUEUE_DB),
        "queue": queue_summary,
        "tasks": task_summary,
        "archives_scanned": len(archive_paths),
        "eligible_count": sum(bool(item.get("eligible")) for item in records),
        "reason_counts": dict(sorted(reasons.items())),
        "archives": records,
    }
    atomic_write_json(output, inventory)
    return inventory


def _group_by_history(paths: list[Path]) -> dict[Path, list[Path]]:
    grouped: dict[Path, list[Path]] = defaultdict(list)
    for path in paths:
        if path.is_dir():
            grouped[path.parent].append(path)
    return grouped


def api_keys() -> dict[str, str]:
    config = read_json(CONFIG_PATH)
    cli = read_json(ROOT / "ovcli.conf")
    return {
        "mayunxiang": str(cli["api_key"]),
        "vikingbot": str(config["bot"]["ov_server"]["api_key"]),
    }


def emit(event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("time", utc_now())
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a") as journal:
        journal.write(line + "\n")
        journal.flush()
        os.fsync(journal.fileno())
    print(line, flush=True)


def load_all_queue_work() -> dict[str, dict[str, int]]:
    """Return active QueueFS rows grouped by queue and state."""
    result: dict[str, dict[str, int]] = defaultdict(dict)
    with sqlite3.connect(f"file:{QUEUE_DB}?mode=ro", uri=True) as db:
        for queue_name, status, count in db.execute(
            "SELECT queue_name, status, COUNT(*) FROM queue_messages "
            "GROUP BY queue_name, status ORDER BY queue_name, status"
        ):
            result[str(queue_name)][str(status)] = int(count)
    return dict(result)


def wait_for_queue_empty(timeout: float, *, all_queues: bool = False) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if all_queues:
            work = load_all_queue_work()
            total = sum(sum(states.values()) for states in work.values())
            detail: Any = work
        else:
            _rows, summary = load_live_queue()
            total = int(summary["total"])
            detail = summary
        if total == 0:
            return
        if time.monotonic() >= deadline:
            scope = "QueueFS" if all_queues else "SessionCommit queue"
            raise TimeoutError(f"{scope} still has {total} messages: {detail}")
        time.sleep(3)


def wait_for_ready(timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "service did not answer"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request("http://127.0.0.1:1933/ready")
            with urllib.request.urlopen(req, timeout=10) as response:
                body = json.load(response)
            if response.status == 200 and body.get("status") == "ready":
                return
            last_error = f"unexpected readiness response: {body}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise TimeoutError(f"OpenViking did not become ready: {last_error}")


def restart_openviking(event: str) -> None:
    subprocess.run(["systemctl", "restart", "openviking.service"], check=True)
    wait_for_ready()
    emit({"event": event})


@contextmanager
def preserve_runtime_config(enabled: bool) -> Iterator[Path | None]:
    """Run recovery with auto-commit paused, then restore exact input bytes.

    QueueFS must be fully idle before either restart.  This keeps a controlled
    maintenance restart from cancelling QueueManager's five-second in-flight
    drain and prevents the restored scheduler from competing with verification.
    """
    if not enabled:
        yield None
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot = BASE / f"runtime-before-recovery-{stamp}.conf"
    original = CONFIG_PATH.read_bytes()
    atomic_write(snapshot, original)
    emit({"event": "runtime_config_captured", "path": str(snapshot)})
    maintenance = json.loads(original)
    memory = maintenance.setdefault("memory", {})
    if not isinstance(memory, dict):
        raise ValueError("ov.conf memory must be an object")
    auto_commit = memory.setdefault("session_auto_commit", {})
    if not isinstance(auto_commit, dict):
        raise ValueError("ov.conf memory.session_auto_commit must be an object")
    auto_commit["default_enabled"] = False
    auto_commit["idle_enabled"] = False
    maintenance_bytes = (
        json.dumps(maintenance, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    maintenance_applied = False
    try:
        wait_for_queue_empty(1800, all_queues=True)
        atomic_write(CONFIG_PATH, maintenance_bytes)
        maintenance_applied = True
        restart_openviking("runtime_maintenance_started")
        yield snapshot
    finally:
        if maintenance_applied:
            # Recovery completion can precede its semantic/vector follow-up.
            # Keep auto-commit paused until all derivative work has settled.
            wait_for_queue_empty(1800, all_queues=True)
        changed = CONFIG_PATH.read_bytes() != original
        atomic_write(CONFIG_PATH, original)
        emit({"event": "runtime_config_restored", "changed": changed})
        if maintenance_applied or changed:
            restart_openviking("runtime_restart_after_restore")


def verify_archive(record: dict[str, Any], key: str) -> tuple[bool, dict[str, Any]]:
    path = Path(record["path"])
    task_path = (
        DATA / "_system/tasks" / record["user"] / f"{record['recovery_task_id']}.json"
    )
    task = read_json(task_path)
    raw_unchanged = (
        path.joinpath("messages.jsonl").is_file()
        and hashlib.sha256(path.joinpath("messages.jsonl").read_bytes()).hexdigest()
        == record["messages_sha256"]
    )
    ambiguous_receipts: list[str] = []
    for receipt_path in path.glob(".long-term-*.json"):
        try:
            status = str(read_json(receipt_path).get("status") or "unknown")
        except Exception:
            status = "unreadable"
        if status != "done":
            ambiguous_receipts.append(f"{receipt_path.name}:{status}")
    missing_files: list[str] = []
    missing_indexes: list[str] = []
    diff_path = path / "memory_diff.json"
    if diff_path.is_file():
        diff = read_json(diff_path)
        operations = diff.get("operations") if isinstance(diff.get("operations"), dict) else {}
        uris = {
            operation["uri"]
            for action in ("adds", "updates")
            for operation in operations.get(action, [])
            if isinstance(operation, dict) and isinstance(operation.get("uri"), str)
        }
        for uri in sorted(uris):
            local = DATA / uri.removeprefix("viking://")
            if not local.is_file():
                missing_files.append(uri)
                continue
            found = request(
                "POST",
                "/api/v1/search/find",
                key,
                {
                    "query": uri.rsplit("/", 1)[-1].removesuffix(".md"),
                    "target_uri": uri,
                    "limit": 1,
                },
            ).get("result", {})
            memories = found.get("memories", []) if isinstance(found, dict) else []
            if not any(item.get("uri") == uri for item in memories if isinstance(item, dict)):
                missing_indexes.append(uri)
    valid = (
        task.get("status") == "completed"
        and (path / ".done").is_file()
        and raw_unchanged
        and not ambiguous_receipts
        and not missing_files
        and not missing_indexes
    )
    return valid, {
        "task_status": task.get("status"),
        "error": str(task.get("error") or "")[:1000],
        "raw_unchanged": raw_unchanged,
        "done_marker": (path / ".done").is_file(),
        "ambiguous_receipts": ambiguous_receipts,
        "missing_files": missing_files,
        "missing_indexes": missing_indexes,
    }


def wait_for_task(record: dict[str, Any], timeout: float) -> dict[str, Any]:
    task_path = (
        DATA / "_system/tasks" / record["user"] / f"{record['recovery_task_id']}.json"
    )
    deadline = time.monotonic() + timeout
    last_status = None
    while True:
        if task_path.is_file():
            task = read_json(task_path)
            status = task.get("status")
            if status != last_status:
                emit(
                    {
                        "event": "recovery_progress",
                        "task_id": record["recovery_task_id"],
                        "status": status,
                    }
                )
                last_status = status
            if status in TERMINAL_TASK_STATES:
                return task
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Recovery task timed out: {record['recovery_task_id']}")
        time.sleep(3)


def select_targets(inventory: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    candidates = [dict(item) for item in inventory.get("archives", []) if item.get("eligible")]
    if limit > 0:
        candidates = candidates[:limit]
    return candidates


def execute_inventory(inventory_path: Path, *, limit: int, timeout: float) -> None:
    inventory = read_json(inventory_path)
    if inventory.get("version") != 2:
        raise ValueError("Recovery inventory version is not supported")
    keys = api_keys()
    targets = select_targets(inventory, limit)
    emit(
        {
            "event": "recovery_start",
            "inventory": str(inventory_path),
            "target_count": len(targets),
        }
    )
    for position, record in enumerate(targets, start=1):
        path = Path(record["path"])
        if not path.joinpath("messages.jsonl").is_file():
            raise RuntimeError(f"Live archive disappeared: {path}")
        actual_hash = hashlib.sha256(path.joinpath("messages.jsonl").read_bytes()).hexdigest()
        if actual_hash != record.get("messages_sha256"):
            raise RuntimeError(f"Raw archive changed since inventory: {path}")

        # Regenerate the whole inventory immediately before every submission.
        fresh_path = BASE / "recovery-inventory-pre-submit.json"
        fresh = generate_inventory(fresh_path)
        fresh_record = next(
            (
                item
                for item in fresh["archives"]
                if item.get("path") == record.get("path")
            ),
            None,
        )
        if not fresh_record or not fresh_record.get("eligible"):
            emit(
                {
                    "event": "skipped_after_reaudit",
                    "path": str(path),
                    "reason": (fresh_record or {}).get("reason", "missing"),
                }
            )
            continue

        user = str(record["user"])
        key = keys.get(user)
        if not key:
            raise RuntimeError(f"No configured API key for recovery user {user}")
        session = urllib.parse.quote(str(record["session"]), safe="")
        archive = urllib.parse.quote(str(record["archive"]), safe="")
        reply = request(
            "POST",
            f"/api/v1/sessions/{session}/archives/{archive}/retry",
            key,
            {
                "expected_messages_sha256": actual_hash,
                "allow_ownerless_ready": record.get("recovery_kind")
                == "ownerless_ready",
            },
        ).get("result", {})
        if not isinstance(reply, dict) or reply.get("status") != "accepted":
            emit(
                {
                    "event": "not_submitted",
                    "path": str(path),
                    "result": reply,
                }
            )
            continue
        record["recovery_task_id"] = reply["task_id"]
        emit(
            {
                "event": "submitted",
                "position": position,
                "path": str(path),
                "task_id": reply["task_id"],
                "recovery_kind": reply.get("recovery_kind"),
            }
        )
        task = wait_for_task(record, timeout)
        valid, checks = verify_archive(record, key)
        emit(
            {
                "event": "verified" if valid else "verification_failed",
                "path": str(path),
                "task_id": reply["task_id"],
                **checks,
            }
        )
        error = str(task.get("error") or "").lower()
        if not valid or any(term in error for term in UNSAFE_ERROR_TERMS):
            raise RuntimeError(f"Recovery safety gate failed: {path}")
    emit({"event": "recovery_finished", "submitted_or_skipped": len(targets)})


def main(args: argparse.Namespace) -> None:
    if args.execute:
        if not args.inventory:
            raise ValueError("--execute requires --inventory")
        with preserve_runtime_config(args.restore_current_config):
            execute_inventory(args.inventory, limit=args.limit, timeout=args.task_timeout)
        return
    output = args.inventory or DEFAULT_INVENTORY
    inventory = generate_inventory(output)
    print(
        json.dumps(
            {
                "event": "inventory_written",
                "path": str(output),
                "archives_scanned": inventory["archives_scanned"],
                "eligible_count": inventory["eligible_count"],
                "queue": inventory["queue"],
                "reason_counts": inventory["reason_counts"],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--task-timeout", type=float, default=2400)
    parser.add_argument(
        "--restore-current-config",
        action="store_true",
        help=(
            "Wait for QueueFS to drain, snapshot this run's ov.conf, temporarily "
            "disable session auto-commit, and restore the exact original bytes on exit."
        ),
    )
    arguments = parser.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)
    lock_path = BASE / "recovery.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            main(arguments)
        except Exception as exc:
            emit(
                {
                    "event": "stop",
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:1000],
                }
            )
            raise
