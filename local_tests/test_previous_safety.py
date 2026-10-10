import asyncio
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openviking.message import Message
from openviking.message.part import TextPart
from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskStatus
from openviking.service.task_work_index import TaskWorkIndex
from openviking.session.memory.context_budget import apply_exact_string_patch, validate_partial_fields
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry as create_default_registry
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.merge_op.base import SearchReplaceBlock, StrPatch
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.archive_store import ArchiveState, ArchiveStore
from openviking.session.session import Session
from openviking.storage.queuefs.session_commit_msg import SessionCommitMsg
from openviking.storage.queuefs.session_commit_processor import SessionCommitProcessor
from openviking_cli.exceptions import FailedPreconditionError, NotFoundError
from openviking_cli.utils.config.vlm_config import VLMConfig


class FakeAgfs:
    def __init__(self):
        self.files = {}
        self.modtimes = {}
        self.read_count = 0

    def ls(self, path, **kwargs):
        prefix = path.rstrip("/") + "/"
        return [
            {
                "name": item_path[len(prefix) :],
                "path": item_path,
                "size": len(payload),
                "modTime": self.modtimes[item_path],
                "isDir": False,
            }
            for item_path, payload in sorted(self.files.items())
            if item_path.startswith(prefix) and "/" not in item_path[len(prefix) :]
        ]

    def read(self, path, **kwargs):
        self.read_count += 1
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path, data, **kwargs):
        self.files[path] = data if isinstance(data, bytes) else data.encode()
        self.modtimes[path] = self.modtimes.get(path, 0) + 1
        return "ok"

    def mkdir(self, path, **kwargs):
        return {}

    def rm(self, path, **kwargs):
        self.files.pop(path, None)
        self.modtimes.pop(path, None)


class MemoryFS:
    def __init__(self):
        self.files = {}

    async def read_file(self, uri, **kwargs):
        if uri not in self.files:
            raise NotFoundError(uri, "file")
        return self.files[uri]

    async def write_file(self, uri, content, **kwargs):
        self.files[uri] = content


class RecoveryFS(MemoryFS):
    def __init__(self):
        super().__init__()
        self.lease = {"lease_ref": "recovery-lease"}
        self._async_agfs = SimpleNamespace(
            pathlock_acquire_tree=AsyncMock(return_value=self.lease),
            pathlock_release=AsyncMock(),
        )

    def _uri_to_path(self, uri, **kwargs):
        return uri

    async def exists(self, uri, **kwargs):
        return uri in self.files

    async def ls(self, uri, **kwargs):
        prefix = uri.rstrip("/") + "/"
        return [
            {"name": path[len(prefix) :]}
            for path in sorted(self.files)
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]

    async def rm(self, uri, **kwargs):
        if uri not in self.files:
            raise NotFoundError(uri, "file")
        del self.files[uri]


def task_payload(task_id, status, updated_at=1):
    return {
        "task_id": task_id,
        "task_type": "session_commit",
        "status": status,
        "created_at": 1,
        "updated_at": updated_at,
        "resource_id": "session",
        "account_id": "default",
        "user_id": "alice",
        "meta": {},
        "stage": status,
        "result": None,
        "error": None,
        "execution_events": None,
        "auth": {},
    }


def put(agfs, task_id, status, updated_at=1):
    path = f"/local/default/_system/tasks/alice/{task_id}.json"
    agfs.write(path, json.dumps(task_payload(task_id, status, updated_at)).encode())
    return path


@unittest.skip('0.4.21 replaces local terminal cache with owner-loop/IO-limited task store; covered by upstream task tracker tests')
class TaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_cache_active_refresh_and_new_file_visibility(self):
        agfs = FakeAgfs()
        done_path = put(agfs, "done", "completed")
        active_path = put(agfs, "active", "pending")
        store = PersistentTaskStore(agfs)

        first = await store.list("default", user_id="alice")
        self.assertEqual({item["task_id"] for item in first}, {"done", "active"})
        self.assertEqual(agfs.read_count, 2)

        second = await store.list("default", user_id="alice")
        self.assertEqual({item["task_id"] for item in second}, {"done", "active"})
        self.assertEqual(agfs.read_count, 3)

        agfs.files[active_path] = json.dumps(
            task_payload("active", "cancelled", 2)
        ).encode()
        agfs.modtimes[active_path] += 1
        put(agfs, "new", "completed")
        third = await store.list("default", user_id="alice")
        self.assertEqual(
            {item["task_id"] for item in third}, {"done", "active", "new"}
        )
        self.assertEqual(
            next(item for item in third if item["task_id"] == "active")["status"],
            "cancelled",
        )
        self.assertEqual(agfs.read_count, 5)

        await store.list("default", user_id="alice")
        self.assertEqual(agfs.read_count, 5)
        self.assertIn(done_path, store._list_cache[("default", "alice")])

    async def test_concurrent_lists_share_one_refresh(self):
        agfs = FakeAgfs()
        put(agfs, "done", "completed")
        store = PersistentTaskStore(agfs)
        original_ls = store._agfs.ls
        entered = 0

        async def delayed_ls(*args, **kwargs):
            nonlocal entered
            entered += 1
            await asyncio.sleep(0.02)
            return await original_ls(*args, **kwargs)

        store._agfs.ls = delayed_ls
        left, right = await asyncio.gather(
            store.list("default", user_id="alice"),
            store.list("default", user_id="alice"),
        )
        self.assertEqual(left, right)
        self.assertEqual(entered, 1)
        self.assertEqual(agfs.read_count, 1)

    async def test_write_and_delete_keep_cache_coherent(self):
        agfs = FakeAgfs()
        put(agfs, "old", "completed")
        store = PersistentTaskStore(agfs)
        await store.list("default", user_id="alice")
        task = SimpleNamespace(**task_payload("local", "completed"), _extra_fields={})
        await store.create(task)
        listed = await store.list("default", user_id="alice")
        self.assertIn("local", {item["task_id"] for item in listed})
        await store.delete("local", account_id="default", user_id="alice")
        listed = await store.list("default", user_id="alice")
        self.assertNotIn("local", {item["task_id"] for item in listed})


class TaskWorkIndexTests(unittest.TestCase):
    def test_rebuild_decodes_queuefs_byte_integer_envelope(self):
        payload = {
            "task_id": "task-byte-envelope",
            "_task_work_id": "work-byte-envelope",
            "account_id": "default",
            "user_id": "alice",
        }
        message = {
            "id": "queue-message",
            "data": list(json.dumps(payload).encode()),
        }
        index = TaskWorkIndex()
        owners = index.rebuild({"SessionCommit": [message]})
        self.assertEqual(owners, {"task-byte-envelope": ("default", "alice")})
        self.assertTrue(index.has_work("task-byte-envelope"))


class MemorySafetyTests(unittest.TestCase):
    def test_memory_type_uses_first_class_field(self):
        memory_file = MemoryFile(
            uri="viking://user/alice/memories/profile.md",
            content="visible",
            memory_type="profile",
            extra_fields={"memory_type": "stale-shadow"},
        )
        validate_partial_fields(
            memory_file, {"memory_type": "profile"}, {"memory_type": ["profile"]}
        )
        with self.assertRaisesRegex(ValueError, "local string patch"):
            validate_partial_fields(
                memory_file,
                {"memory_type": "stale-shadow"},
                {"memory_type": ["profile"]},
            )

    def test_exact_patch_rejects_duplicate_or_unread_matches(self):
        current = "same\nvisible unique\nsame\n"
        with self.assertRaisesRegex(ValueError, "exactly once"):
            apply_exact_string_patch(
                current, {"blocks": [{"search": "same", "replace": "changed"}]}
            )
        with self.assertRaisesRegex(ValueError, "visible"):
            apply_exact_string_patch(
                current,
                {"blocks": [{"search": "visible unique", "replace": "changed"}]},
                ["another visible span"],
            )

class VLMConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_vlm_config_forwards_per_call_output_cap(self):
        backend = SimpleNamespace(get_completion_async=AsyncMock(return_value="ok"))
        config = VLMConfig.model_construct(thinking=False)
        config._vlm_instance = backend

        result = await config.get_completion_async(
            prompt="bounded",
            tools=[{"type": "function"}],
            tool_choice="auto",
            max_tokens=16_000,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(backend.get_completion_async.await_args.kwargs["max_tokens"], 16_000)

class MemoryUpdaterSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_patch_preserves_unread_body_in_actual_updater(self):
        fs = MemoryFS()
        uri = "viking://user/alice/memories/profile.md"
        original = MemoryFile(
            uri=uri,
            content="visible unique\nunread important\n",
            memory_type="profile",
        )
        fs.files[uri] = MemoryFileUtils.write(original)
        updater = MemoryUpdater(registry=create_default_registry(), vikingdb=None)
        updater._get_viking_fs = lambda: fs
        operation = ResolvedOperation(
            old_memory_file_content=original,
            memory_type="profile",
            uris=[uri],
            memory_fields={
                "content": StrPatch(
                    blocks=[
                        SearchReplaceBlock(
                            search="visible unique", replace="visible changed"
                        )
                    ]
                )
            },
            partial_read_fields={"content": ["visible unique\n"]},
        )

        await updater._apply_upsert(operation, SimpleNamespace())
        saved = MemoryFileUtils.read(fs.files[uri], uri=uri)
        self.assertIn("visible changed", saved.plain_content())
        self.assertIn("unread important", saved.plain_content())

    async def test_transient_read_failure_does_not_fall_back_to_stale_snapshot(self):
        fs = MemoryFS()
        fs.read_file = AsyncMock(side_effect=TimeoutError("storage unavailable"))
        uri = "viking://user/alice/memories/profile.md"
        original = MemoryFile(
            uri=uri,
            content="stale snapshot",
            memory_type="profile",
        )
        updater = MemoryUpdater(registry=create_default_registry(), vikingdb=None)
        updater._get_viking_fs = lambda: fs
        operation = ResolvedOperation(
            old_memory_file_content=original,
            memory_type="profile",
            uris=[uri],
            memory_fields={"content": "replacement"},
        )

        with self.assertRaisesRegex(TimeoutError, "storage unavailable"):
            await updater._apply_upsert(operation, SimpleNamespace())
        self.assertFalse(fs.files)


def commit_message():
    return SessionCommitMsg(
        task_id="task-1",
        session_id="session",
        session_uri="viking://user/alice/sessions/session",
        archive_uri="viking://user/alice/sessions/session/history/archive_001",
        user={"account_id": "default", "user_id": "alice"},
    )


class ArchiveRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fs = RecoveryFS()
        self.archive_uri = "viking://user/alice/sessions/session/history/archive_001"
        self.message = Message(
            id="message-1", role="user", parts=[TextPart(text="preserve me")]
        )
        self.raw_messages = json.dumps(self.message.to_dict(), ensure_ascii=False) + "\n"
        self.messages_sha256 = hashlib.sha256(self.raw_messages.encode()).hexdigest()
        self.original_msg = commit_message()
        self.phase1_meta = {
            "phase1": {
                "status": "ready",
                "queue_message": self.original_msg.to_dict(),
            }
        }
        self.fs.files[f"{self.archive_uri}/messages.jsonl"] = self.raw_messages
        self.fs.files[f"{self.archive_uri}/.meta.json"] = json.dumps(self.phase1_meta)
        self.session = object.__new__(Session)
        self.session._viking_fs = self.fs
        self.session._archive_meta_merge_lock = asyncio.Lock()
        self.session._session_uri = "viking://user/alice/sessions/session"
        self.session.session_id = "session"
        self.session._archives = ArchiveStore(
            self.fs, SimpleNamespace(account_id="default"), self.session._session_uri
        )
        self.session.ctx = SimpleNamespace(
            account_id="default",
            user=SimpleNamespace(
                user_id="alice",
                to_dict=lambda: {"account_id": "default", "user_id": "alice"},
            ),
        )
        self.tracker = SimpleNamespace(
            MAX_TASKS=10_000,
            list_tasks=AsyncMock(return_value=[]),
            get=AsyncMock(return_value=None),
            has_work=Mock(return_value=False),
            fail=AsyncMock(),
            create=AsyncMock(return_value=SimpleNamespace(status=TaskStatus.PENDING)),
            complete=AsyncMock(),
        )
        self.queue = SimpleNamespace(snapshot=AsyncMock(return_value=[]))
        self.queue_manager = SimpleNamespace(
            get_queue=Mock(return_value=self.queue), enqueue=AsyncMock()
        )

    def _state(self, state="pending", failed=None):
        return ArchiveState(
            archive_id="archive_001",
            archive_uri=self.archive_uri,
            index=1,
            state=state,
            failed=failed or {},
        )

    def _patch_runtime(self, state):
        self.session._archives.scan_states = AsyncMock(return_value=[state])
        return (
            patch(
                "openviking.service.task_tracker.get_task_tracker",
                return_value=self.tracker,
            ),
            patch(
                "openviking.storage.queuefs.get_queue_manager",
                return_value=self.queue_manager,
            ),
        )

    async def _retry(self, state, **kwargs):
        tracker_patch, queue_patch = self._patch_runtime(state)
        with tracker_patch, queue_patch:
            return await self.session.retry_archive(
                "archive_001",
                expected_messages_sha256=kwargs.pop(
                    "expected_messages_sha256", self.messages_sha256
                ),
                **kwargs,
            )

    async def test_queue_message_recovery_field_is_backward_compatible(self):
        legacy = self.original_msg.to_dict()
        legacy.pop("recovery")
        self.assertEqual(SessionCommitMsg.from_dict(legacy).recovery, {})
        legacy["future_field"] = "ignored"
        restored = SessionCommitMsg.from_dict(legacy)
        self.assertNotIn("future_field", restored.to_dict())

        recovery = {"kind": "failed", "messages_sha256": self.messages_sha256}
        current = SessionCommitMsg.from_dict(
            {**self.original_msg.to_dict(), "recovery": recovery}
        )
        self.assertEqual(current.to_dict()["recovery"], recovery)

    async def test_ownerless_ready_requires_explicit_authorization(self):
        before = dict(self.fs.files)
        with self.assertRaisesRegex(FailedPreconditionError, "allow_ownerless_ready"):
            await self._retry(self._state())
        self.assertEqual(self.fs.files, before)
        self.tracker.create.assert_not_awaited()
        self.queue_manager.enqueue.assert_not_awaited()

    async def test_hash_mismatch_has_no_persistent_side_effects(self):
        before = dict(self.fs.files)
        with self.assertRaisesRegex(FailedPreconditionError, "messages changed"):
            await self._retry(
                self._state(),
                expected_messages_sha256="0" * 64,
                allow_ownerless_ready=True,
            )
        self.assertEqual(self.fs.files, before)
        self.tracker.create.assert_not_awaited()
        self.queue_manager.enqueue.assert_not_awaited()

    async def test_queue_archive_and_session_owners_skip_recovery(self):
        archived_payload = {
            "archive_uri": self.archive_uri,
            "task_id": "archive-owner",
        }
        cases = (
            (
                {"data": list(json.dumps(archived_payload).encode())},
                "archive_owned",
            ),
            (
                {
                    "session_uri": self.session._session_uri,
                    "archive_uri": self.archive_uri + "-other",
                    "task_id": "session-owner",
                },
                "session_busy",
            ),
        )
        for payload, expected_reason in cases:
            with self.subTest(expected_reason):
                self.queue.snapshot.return_value = [{"data": json.dumps(payload)}]
                result = await self._retry(self._state(), allow_ownerless_ready=True)
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], expected_reason)
        self.tracker.create.assert_not_awaited()
        self.queue_manager.enqueue.assert_not_awaited()

    async def test_cancelled_task_and_applying_receipt_are_rejected(self):
        self.tracker.get.return_value = SimpleNamespace(status=TaskStatus.CANCELLED)
        with self.assertRaisesRegex(FailedPreconditionError, "Cancelled archives"):
            await self._retry(self._state(), allow_ownerless_ready=True)

        self.tracker.get.return_value = None
        self.fs.files[f"{self.archive_uri}/.long-term-message-1.json"] = json.dumps(
            {"status": "applying", "message_ids": ["message-1"]}
        )
        with self.assertRaisesRegex(FailedPreconditionError, "ambiguous long-term"):
            await self._retry(self._state(), allow_ownerless_ready=True)
        self.tracker.create.assert_not_awaited()
        self.queue_manager.enqueue.assert_not_awaited()

    async def test_cancelled_failure_requires_explicit_opt_in(self):
        failure = {"stage": "cancelled", "error": "session commit cancelled"}
        failed_raw = json.dumps(failure)
        self.fs.files[f"{self.archive_uri}/.failed.json"] = failed_raw

        with self.assertRaisesRegex(FailedPreconditionError, "explicit review"):
            await self._retry(self._state("failed", failure))

        self.tracker.create.reset_mock()
        self.queue_manager.enqueue.reset_mock()
        with patch(
            "openviking.session.session.uuid4", return_value="cancel-recovery-task"
        ):
            result = await self._retry(
                self._state("failed", failure),
                allow_cancelled_failure=True,
            )

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["recovery_kind"], "failed")
        self.assertEqual(
            self.fs.files[
                f"{self.archive_uri}/.failure-before-cancel-recovery-task.json"
            ],
            failed_raw,
        )
        self.tracker.create.assert_awaited_once()
        self.queue_manager.enqueue.assert_awaited_once()

    async def test_done_receipt_is_promoted_and_failed_archive_is_enqueued(self):
        failure = {"stage": "working_memory", "error": "context limit"}
        failed_raw = json.dumps(failure)
        self.fs.files[f"{self.archive_uri}/.failed.json"] = failed_raw
        self.fs.files[f"{self.archive_uri}/memory_diff.json"] = "{}"
        self.fs.files[f"{self.archive_uri}/.long-term-message-1.json"] = json.dumps(
            {"status": "done", "message_ids": ["message-1"]}
        )

        with patch(
            "openviking.session.session.uuid4", return_value="recovery-task"
        ):
            result = await self._retry(self._state("failed", failure))

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["recovery_kind"], "failed")
        self.assertEqual(result["completed_memory_steps"], {"long_term": ["message-1"]})
        saved_meta = json.loads(self.fs.files[f"{self.archive_uri}/.meta.json"])
        self.assertEqual(
            saved_meta["completed_memory_steps"], {"long_term": ["message-1"]}
        )
        self.assertEqual(
            self.fs.files[f"{self.archive_uri}/.failure-before-recovery-task.json"],
            failed_raw,
        )
        enqueued = self.queue_manager.enqueue.await_args.args[1]
        self.assertEqual(enqueued["recovery"]["task_id"], "recovery-task")
        self.assertEqual(enqueued["recovery"]["messages_sha256"], self.messages_sha256)

    async def test_consumer_rejects_recovery_ownership_mismatch_and_consumes(self):
        msg = commit_message()
        msg.task_id = "recovery-task"
        msg.recovery = {
            "version": 1,
            "kind": "failed",
            "task_id": msg.task_id,
            "previous_task_id": "task-1",
            "messages_sha256": self.messages_sha256,
        }
        bad_meta = dict(self.phase1_meta)
        bad_meta["recovery"] = {**msg.recovery, "task_id": "another-task"}
        self.fs.files[f"{self.archive_uri}/.meta.json"] = json.dumps(bad_meta)
        with patch(
            "openviking.service.task_tracker.get_task_tracker",
            return_value=self.tracker,
        ):
            processed = await self.session.resume_queued_commit(msg)
        self.assertTrue(processed)
        self.tracker.fail.assert_awaited_once()
        self.assertIn("does not own", self.tracker.fail.await_args.args[1])

    async def test_consumer_redelivery_requires_matching_failure_backup(self):
        msg = commit_message()
        msg.task_id = "recovery-task"
        msg.recovery = {
            "version": 1,
            "kind": "failed",
            "task_id": msg.task_id,
            "previous_task_id": "task-1",
            "messages_sha256": self.messages_sha256,
        }
        failed_raw = json.dumps({"stage": "working_memory", "error": "timeout"})
        persisted = {
            **msg.recovery,
            "failed_sha256": hashlib.sha256(failed_raw.encode()).hexdigest(),
        }
        meta = dict(self.phase1_meta)
        meta["recovery"] = persisted
        self.fs.files[f"{self.archive_uri}/.meta.json"] = json.dumps(meta)

        with self.assertRaisesRegex(FailedPreconditionError, "without a recovery backup"):
            await self.session._activate_archive_recovery(msg)

        backup_uri = f"{self.archive_uri}/.failure-before-{msg.task_id}.json"
        self.fs.files[backup_uri] = failed_raw
        await self.session._activate_archive_recovery(msg)
        self.fs.files[backup_uri] = failed_raw + "changed"
        with self.assertRaisesRegex(FailedPreconditionError, "backup changed"):
            await self.session._activate_archive_recovery(msg)

    async def test_consumer_does_not_ack_transient_recovery_storage_failure(self):
        msg = commit_message()
        msg.recovery = {
            "version": 1,
            "kind": "failed",
            "task_id": msg.task_id,
            "previous_task_id": "old-task",
            "messages_sha256": self.messages_sha256,
        }
        self.session._activate_archive_recovery = AsyncMock(
            side_effect=OSError("temporary storage failure")
        )
        with patch(
            "openviking.service.task_tracker.get_task_tracker",
            return_value=self.tracker,
        ):
            with self.assertRaisesRegex(OSError, "temporary storage"):
                await self.session.resume_queued_commit(msg)
        self.tracker.fail.assert_not_awaited()

    async def test_activation_propagates_transient_metadata_read_failure(self):
        msg = commit_message()
        msg.recovery = {
            "version": 1,
            "kind": "failed",
            "task_id": msg.task_id,
            "previous_task_id": "old-task",
            "messages_sha256": self.messages_sha256,
        }
        original_read = self.fs.read_file

        async def fail_meta(uri, **kwargs):
            if uri.endswith("/.meta.json"):
                raise OSError("temporary metadata storage failure")
            return await original_read(uri, **kwargs)

        self.fs.read_file = fail_meta
        with self.assertRaisesRegex(OSError, "temporary metadata"):
            await self.session._activate_archive_recovery(msg)


class QueueStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def _invoke(self, result):
        processor = SessionCommitProcessor(Mock())
        processor._process = AsyncMock(return_value=result)
        raw = {"data": json.dumps(commit_message().to_dict())}
        return await processor.on_dequeue(raw)

    async def test_business_failure_reports_queue_error(self):
        from openviking.storage.queuefs.process_result import ProcessOutcome
        result = await self._invoke((True, "memory extraction failed"))
        self.assertIs(result.outcome, ProcessOutcome.FAILED)
        self.assertEqual(result.error, "memory extraction failed")

    async def test_successful_dequeue_reports_success(self):
        from openviking.storage.queuefs.process_result import ProcessOutcome
        result = await self._invoke((True, None))
        self.assertIs(result.outcome, ProcessOutcome.SUCCESS)

    async def test_requeued_dequeue_is_not_also_reported_successful(self):
        from openviking.storage.queuefs.process_result import ProcessOutcome
        result = await self._invoke((False, None))
        self.assertIs(result.outcome, ProcessOutcome.REQUEUED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
