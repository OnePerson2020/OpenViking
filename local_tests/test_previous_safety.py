import asyncio
import concurrent.futures
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openviking.message import Message
from openviking.message.part import ContextPart, TextPart, ToolPart
from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskStatus
from openviking.service.task_work_index import TaskWorkIndex
from openviking.session.extraction_batch import ExtractionBatchLimits
from openviking.session.memory.context_budget import (
    MemoryInputBudgetError,
    apply_exact_string_patch,
    json_tokens,
    split_rendered_message_batches,
    validate_partial_fields,
)
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry as create_default_registry
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.merge_op.base import SearchReplaceBlock, StrPatch
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session import working_memory as wm
from openviking.session.archive_store import ArchiveState, ArchiveStore
from openviking.session.session import (
    WM_SEVEN_SECTIONS,
    WM_UPDATE_TOOL,
    Session,
    _ArchiveSummaryResult,
    _CheckpointRequest,
)
from openviking.storage.queuefs.session_commit_msg import SessionCommitMsg
from openviking.storage.queuefs.session_commit_processor import SessionCommitProcessor
from openviking_cli.exceptions import FailedPreconditionError, NotFoundError
from openviking_cli.utils.config.vlm_config import VLMConfig


def complete_wm(label: str, *, bulk: str = "") -> str:
    return "\n\n".join(
        f"## {section}\n{label} {bulk if index == 0 else section}"
        for index, section in enumerate(WM_SEVEN_SECTIONS)
    )


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

    def test_lossless_wm_split_preserves_original(self):
        text = ("text-片段\n" * 700) + "TEXT-END"
        tool_output = ("tool-output\n" * 900) + "TOOL-END"
        abstract = ("context摘要\n" * 700) + "CONTEXT-END"
        message = Message(
            id="source",
            role="assistant",
            created_at="2026-09-15T00:00:00Z",
            parts=[
                TextPart(text=text),
                ToolPart(tool_name="lookup", tool_output=tool_output),
                ContextPart(uri="viking://context", abstract=abstract),
            ],
        )
        original = message.to_dict()
        batches = split_rendered_message_batches(
            [message],
            512,
            render_message=wm.format_message_for_wm,
            render_batch=lambda batch: wm.format_messages_for_wm(batch, []),
        )
        fragments = [fragment for batch in batches for fragment in batch]
        self.assertGreater(len(batches), 3)
        self.assertEqual(message.to_dict(), original)
        self.assertEqual(
            "".join(
                part.text
                for fragment in fragments
                for part in fragment.parts
                if isinstance(part, TextPart)
            ),
            text,
        )
        self.assertEqual(
            "".join(
                part.tool_output
                for fragment in fragments
                for part in fragment.parts
                if isinstance(part, ToolPart)
            ),
            tool_output,
        )
        self.assertEqual(
            "".join(
                part.abstract
                for fragment in fragments
                for part in fragment.parts
                if isinstance(part, ContextPart)
            ),
            abstract,
        )

    def test_oversized_multipart_omits_only_render_invisible_empty_parts(self):
        from openviking.utils.token_estimation import estimate_text_tokens

        message = Message(
            id="multipart",
            role="assistant",
            created_at="2026-09-15T00:00:00Z",
            parts=[
                TextPart(text=""),
                ContextPart(uri="viking://empty", abstract=""),
                ToolPart(tool_name="", tool_output="INVISIBLE-UNNAMED-" * 1000),
                TextPart(text=("visible text\n" * 800) + "TEXT-END"),
                ToolPart(
                    tool_name="named-tool",
                    tool_output=("visible tool\n" * 800) + "TOOL-END",
                    tool_status="completed",
                ),
                ContextPart(
                    uri="viking://context",
                    abstract=("visible context\n" * 800) + "CONTEXT-END",
                ),
            ],
        )
        original = message.to_dict()
        batches = split_rendered_message_batches(
            [message],
            512,
            render_message=wm.format_message_for_wm,
            render_batch=lambda batch: wm.format_messages_for_wm(batch, []),
        )
        fragments = [fragment for batch in batches for fragment in batch]
        rendered = "\n".join(
            wm.format_message_for_wm(fragment) for fragment in fragments
        )

        self.assertEqual(message.to_dict(), original)
        self.assertNotIn("(no content)", rendered)
        self.assertNotIn("INVISIBLE-UNNAMED-", rendered)
        self.assertTrue(all(fragment.parts for fragment in fragments))
        self.assertTrue(
            all(
                estimate_text_tokens(wm.format_messages_for_wm(batch, []))
                <= 512
                for batch in batches
            )
        )

    def test_request_budget_includes_tools_and_choice(self):
        budget = 48_000
        tool_choice = {
            "type": "function",
            "function": {"name": "update_working_memory"},
        }
        low, high = 0, 240_000
        while low < high:
            middle = (low + high + 1) // 2
            if Session._working_memory_request_tokens("x" * middle) <= budget:
                low = middle
            else:
                high = middle - 1
        prompt = "x" * low
        self.assertLessEqual(Session._working_memory_request_tokens(prompt), budget)
        self.assertGreater(
            Session._working_memory_request_tokens(
                prompt, tools=[WM_UPDATE_TOOL], tool_choice=tool_choice
            ),
            budget,
        )

    def test_creation_requires_exact_seven_sections(self):
        Session._validate_complete_working_memory(complete_wm("ok"))
        missing = complete_wm("bad").replace("## Open Issues", "### Open Issues")
        with self.assertRaisesRegex(ValueError, "seven required"):
            Session._validate_complete_working_memory(missing)
        duplicate = complete_wm("bad") + "\n\n## Open Issues\nduplicate"
        with self.assertRaisesRegex(ValueError, "seven required"):
            Session._validate_complete_working_memory(duplicate)


class WorkingMemoryAsyncTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_budget_failure_happens_before_network(self):
        network = AsyncMock()
        config = SimpleNamespace(
            output_language_override="en",
            memory=SimpleNamespace(extraction_input_token_budget=48_000),
            vlm=SimpleNamespace(is_available=lambda: True, get_completion_async=network),
        )
        prompt = "x" * 220_000
        prior = complete_wm("prior")
        messages = [Message(id="m", role="user", parts=[TextPart(text="fact")])]
        with patch(
            "openviking.session.session.get_openviking_config", return_value=config
        ), patch("openviking.prompts.render_prompt", return_value=prompt):
            with self.assertRaisesRegex(
                MemoryInputBudgetError, "Working Memory input budget exceeded"
            ):
                await Session._generate_archive_summary_async(
                    object.__new__(Session), messages, latest_archive_overview=prior
                )
        network.assert_not_awaited()

    async def test_creation_failure_is_not_replaced_by_synthetic_summary(self):
        malformed = complete_wm("bad").replace("## Open Issues", "### Open Issues")
        network = AsyncMock(return_value=malformed)
        config = SimpleNamespace(
            output_language_override="en",
            memory=SimpleNamespace(extraction_input_token_budget=48_000),
            vlm=SimpleNamespace(is_available=lambda: True, get_completion_async=network),
        )
        messages = [Message(id="m", role="user", parts=[TextPart(text="fact")])]
        with patch(
            "openviking.session.session.get_openviking_config", return_value=config
        ), patch("openviking.prompts.render_prompt", return_value="bounded"):
            with self.assertRaisesRegex(ValueError, "seven required"):
                await Session._generate_archive_summary_async(
                    object.__new__(Session), messages
                )
        # 2026-09-21: one bounded format retry; still no synthetic fallback.
        self.assertEqual(network.await_count, 2)
        self.assertEqual(network.await_args.kwargs["max_tokens"], 16_000)

    async def test_batch_failure_resumes_without_publishing_partial_overview(self):
        fs = MemoryFS()
        session = object.__new__(Session)
        session._viking_fs = fs
        session.ctx = SimpleNamespace()
        messages = [
            Message(id=marker, role="user", parts=[TextPart(text=marker * 7000)])
            for marker in ("A", "B", "C")
        ]
        archive_uri = "viking://user/u/sessions/s/history/archive_001"
        calls = []

        async def fail_second(batch, latest_archive_overview="", checkpoint_requests=None):
            calls.append((batch[0].id, latest_archive_overview))
            if len(calls) == 2:
                raise RuntimeError("upstream")
            return _ArchiveSummaryResult(overview=complete_wm(f"attempt-{len(calls)}"))

        session._generate_archive_summary_async = AsyncMock(side_effect=fail_second)
        config = SimpleNamespace(memory=SimpleNamespace(extraction_input_token_budget=48_000))
        with patch(
            "openviking.session.session.get_openviking_config", return_value=config
        ):
            with self.assertRaisesRegex(RuntimeError, "upstream"):
                await session._generate_archive_summary_with_budget(
                    messages,
                    latest_archive_overview=complete_wm("seed"),
                    archive_uri=archive_uri,
                    limits=ExtractionBatchLimits(max_message_tokens=1_000),
                )

        progress_uri = f"{archive_uri}/.working-memory-progress.json"
        progress = json.loads(fs.files[progress_uri])
        self.assertEqual(progress["completed_batches"], 1)
        self.assertEqual(progress["status"], "processing")
        self.assertNotIn(f"{archive_uri}/.overview.md", fs.files)

        retry_calls = []

        async def succeed(batch, latest_archive_overview="", checkpoint_requests=None):
            retry_calls.append((batch[0].id, latest_archive_overview))
            return _ArchiveSummaryResult(overview=complete_wm(f"retry-{len(retry_calls)}"))

        session._generate_archive_summary_async = AsyncMock(side_effect=succeed)
        with patch(
            "openviking.session.session.get_openviking_config", return_value=config
        ):
            result = await session._generate_archive_summary_with_budget(
                messages,
                latest_archive_overview=complete_wm("seed"),
                archive_uri=archive_uri,
                limits=ExtractionBatchLimits(max_message_tokens=1_000),
            )
        self.assertEqual(retry_calls[0][0], calls[1][0])
        self.assertEqual(json.loads(fs.files[progress_uri])["status"], "done")
        self.assertEqual(result.wm_batch_count, len(calls[:1]) + len(retry_calls))

    async def test_oversized_output_is_fitted_and_advances_progress(self):
        fs = MemoryFS()
        session = object.__new__(Session)
        session._viking_fs = fs
        session.ctx = SimpleNamespace()
        archive_uri = "viking://user/u/sessions/s/history/archive_output"
        messages = [
            Message(id=marker, role="user", parts=[TextPart(text=marker * 7000)])
            for marker in ("A", "B")
        ]
        calls = 0

        async def fold(batch, latest_archive_overview="", checkpoint_requests=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _ArchiveSummaryResult(overview=complete_wm("valid"))
            return _ArchiveSummaryResult(overview=complete_wm("huge", bulk="超" * 70_000))

        session._generate_archive_summary_async = AsyncMock(side_effect=fold)
        config = SimpleNamespace(memory=SimpleNamespace(extraction_input_token_budget=48_000))
        with patch(
            "openviking.session.session.get_openviking_config", return_value=config
        ):
            result = await session._generate_archive_summary_with_budget(
                messages,
                latest_archive_overview=complete_wm("seed"),
                archive_uri=archive_uri,
                limits=ExtractionBatchLimits(max_message_tokens=1_000),
            )
        Session._validate_complete_working_memory(result.overview)
        Session._guard_working_memory_output(result.overview, ())
        self.assertIn("truncated to Working Memory budget", result.overview)
        saved = json.loads(fs.files[f"{archive_uri}/.working-memory-progress.json"])
        self.assertEqual(saved["completed_batches"], saved["batch_count"])
        self.assertEqual(saved["status"], "done")

    async def test_checkpoint_accumulates_across_split_batches(self):
        session = object.__new__(Session)
        session._viking_fs = None
        session._read_working_memory_progress = AsyncMock(return_value={})
        session._write_working_memory_progress = AsyncMock()
        history = []

        async def fold(batch, latest_archive_overview="", checkpoint_requests=None):
            previous = checkpoint_requests[0].previous_checkpoint_abstract
            history.append(previous)
            current = f"{previous}|batch-{len(history)}"
            return _ArchiveSummaryResult(
                overview=complete_wm(f"wm-{len(history)}"),
                checkpoint_summaries=(current,),
            )

        session._generate_archive_summary_async = AsyncMock(side_effect=fold)
        messages = [
            Message(
                id="shared-source",
                role="assistant",
                parts=[TextPart(text="S" * 8_000)],
            )
        ]
        requests = [
            _CheckpointRequest(
                turn_anchor_message_id="anchor",
                source_message_ids=("shared-source",),
                retained_message_token_budget=256,
                estimated_active_tokens=512,
                previous_checkpoint_abstract="prior",
            )
        ]
        result = await session._generate_archive_summary_with_budget(
            messages,
            latest_archive_overview=complete_wm("seed"),
            checkpoint_requests=requests,
            archive_uri="viking://archive",
            limits=ExtractionBatchLimits(max_message_tokens=512),
        )

        self.assertGreater(len(history), 1)
        self.assertEqual(history[0], "prior")
        for previous, current in zip(history, history[1:]):
            self.assertTrue(current.startswith(previous + "|batch-"))
        self.assertEqual(result.checkpoint_summaries, (f"{history[-1]}|batch-{len(history)}",))

    async def test_empty_checkpoint_does_not_advance_and_retries_same_batch(self):
        fs = MemoryFS()
        session = object.__new__(Session)
        session._viking_fs = fs
        session.ctx = SimpleNamespace()
        archive_uri = "viking://user/u/sessions/s/history/archive_checkpoint"
        messages = [
            Message(
                id="shared-source",
                role="assistant",
                parts=[TextPart(text="S" * 8_000)],
            )
        ]
        requests = [
            _CheckpointRequest(
                turn_anchor_message_id="anchor",
                source_message_ids=("shared-source",),
                retained_message_token_budget=256,
                estimated_active_tokens=512,
            )
        ]
        attempted_batches = []

        async def fail_second(batch, latest_archive_overview="", checkpoint_requests=None):
            attempted_batches.append(wm.format_messages_for_wm(batch, []))
            index = len(attempted_batches)
            return _ArchiveSummaryResult(
                overview=complete_wm(f"attempt-{index}"),
                checkpoint_summaries=("checkpoint-1" if index == 1 else "   ",),
            )

        session._generate_archive_summary_async = AsyncMock(side_effect=fail_second)
        with self.assertRaisesRegex(ValueError, "empty checkpoint summary"):
            await session._generate_archive_summary_with_budget(
                messages,
                latest_archive_overview=complete_wm("seed"),
                checkpoint_requests=requests,
                archive_uri=archive_uri,
                limits=ExtractionBatchLimits(max_message_tokens=512),
            )

        progress_uri = f"{archive_uri}/.working-memory-progress.json"
        progress = json.loads(fs.files[progress_uri])
        self.assertEqual(progress["completed_batches"], 1)
        self.assertEqual(progress["checkpoint_summaries"], {"anchor": "checkpoint-1"})
        failed_batch = attempted_batches[1]
        retry_batches = []

        async def retry(batch, latest_archive_overview="", checkpoint_requests=None):
            retry_batches.append(wm.format_messages_for_wm(batch, []))
            if len(retry_batches) == 1:
                self.assertEqual(
                    checkpoint_requests[0].previous_checkpoint_abstract,
                    "checkpoint-1",
                )
            return _ArchiveSummaryResult(
                overview=complete_wm(f"retry-{len(retry_batches)}"),
                checkpoint_summaries=(f"checkpoint-retry-{len(retry_batches)}",),
            )

        session._generate_archive_summary_async = AsyncMock(side_effect=retry)
        result = await session._generate_archive_summary_with_budget(
            messages,
            latest_archive_overview=complete_wm("seed"),
            checkpoint_requests=requests,
            archive_uri=archive_uri,
            limits=ExtractionBatchLimits(max_message_tokens=512),
        )
        self.assertEqual(retry_batches[0], failed_batch)
        self.assertTrue(result.checkpoint_summaries[0].startswith("checkpoint-retry-"))
        self.assertEqual(json.loads(fs.files[progress_uri])["status"], "done")

    async def test_bad_progress_checksum_restarts_from_batch_zero(self):
        fs = MemoryFS()
        session = object.__new__(Session)
        session._viking_fs = fs
        session.ctx = SimpleNamespace()
        archive_uri = "viking://user/u/sessions/s/history/archive_bad_progress"
        progress_uri = f"{archive_uri}/.working-memory-progress.json"
        fs.files[progress_uri] = json.dumps(
            {
                "version": 1,
                "status": "processing",
                "plan_hash": "stale",
                "completed_batches": 1,
                "batch_count": 2,
                "overview": complete_wm("stale"),
                "checkpoint_summaries": {},
                "checksum": "0" * 64,
            }
        )
        seen = []

        async def fold(batch, latest_archive_overview="", checkpoint_requests=None):
            seen.append(latest_archive_overview)
            return _ArchiveSummaryResult(overview=complete_wm(f"batch-{len(seen)}"))

        session._generate_archive_summary_async = AsyncMock(side_effect=fold)
        await session._generate_archive_summary_with_budget(
            [Message(id="source", role="user", parts=[TextPart(text="A" * 8_000)])],
            latest_archive_overview=complete_wm("seed"),
            archive_uri=archive_uri,
            limits=ExtractionBatchLimits(max_message_tokens=512),
        )
        self.assertGreater(len(seen), 1)
        self.assertEqual(seen[0], complete_wm("seed"))
        saved = json.loads(fs.files[progress_uri])
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["completed_batches"], len(seen))
        self.assertEqual(saved["checksum"], Session._working_memory_progress_checksum(saved))


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


def load_recovery_script():
    path = Path(__file__).with_name("recover_archives.py")
    spec = importlib.util.spec_from_file_location("recover_archives_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class RecoveryInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recovery = load_recovery_script()

    def test_decode_queuefs_byte_integer_envelope(self):
        payload = commit_message().to_dict()
        inner = json.dumps(payload).encode()
        envelope = json.dumps({"data": list(inner)})
        self.assertEqual(self.recovery.decode_queue_payload(envelope), payload)

    def test_inventory_classifies_safety_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "viking/default"
            archive = data / "user/alice/sessions/session/history/archive_001"
            archive.mkdir(parents=True)
            raw_message = json.dumps(
                Message(id="m1", role="user", parts=[TextPart(text="raw")]).to_dict()
            )
            (archive / "messages.jsonl").write_text(raw_message + "\n")
            (archive / ".meta.json").write_text(
                json.dumps(
                    {
                        "phase1": {
                            "status": "ready",
                            "queue_message": commit_message().to_dict(),
                        }
                    }
                )
            )
            kwargs = {
                "queue_by_archive": {},
                "queue_by_session": {},
                "tasks": {},
                "covered": set(),
            }
            with patch.object(self.recovery, "DATA", data):
                ownerless = self.recovery.audit_archive(archive, **kwargs)
                self.assertTrue(ownerless["eligible"])
                self.assertEqual(ownerless["recovery_kind"], "ownerless_ready")

                covered = self.recovery.audit_archive(
                    archive, **{**kwargs, "covered": {"archive_001"}}
                )
                self.assertEqual(covered["reason"], "already_covered")

                cancelled = self.recovery.audit_archive(
                    archive,
                    **{
                        **kwargs,
                        "tasks": {"task-1": {"status": "cancelled"}},
                    },
                )
                self.assertEqual(cancelled["reason"], "original_task_cancelled")

                (archive / ".long-term-m1.json").write_text(
                    json.dumps({"status": "applying", "message_ids": ["m1"]})
                )
                applying = self.recovery.audit_archive(archive, **kwargs)
                self.assertEqual(
                    applying["reason"], "ambiguous_long_term_receipt:applying"
                )

                (archive / ".long-term-m1.json").unlink()
                (archive / ".failed.json").write_text(
                    json.dumps(
                        {
                            "stage": "memory_extraction",
                            "error": "Total tokens of image and text exceed max message tokens",
                        }
                    )
                )
                image_error = self.recovery.audit_archive(archive, **kwargs)
                self.assertEqual(
                    image_error["reason"], "failure_requires_manual_review"
                )

                (archive / ".failed.json").write_text(
                    json.dumps(
                        {
                            "stage": "memory_extraction",
                            "error": "Error code: 429 ServerOverloaded",
                        }
                    )
                )
                overload = self.recovery.audit_archive(archive, **kwargs)
                self.assertTrue(overload["eligible"])
                self.assertEqual(overload["recovery_kind"], "failed")

                (archive / ".failed.json").write_text(
                    json.dumps(
                        {
                            "stage": "memory_extraction",
                            "error": (
                                "VLMConfig.get_completion_async() got an unexpected "
                                "keyword argument 'max_tokens'"
                            ),
                        }
                    )
                )
                fixed_proxy_error = self.recovery.audit_archive(archive, **kwargs)
                self.assertTrue(fixed_proxy_error["eligible"])
                self.assertEqual(fixed_proxy_error["recovery_kind"], "failed")

                (archive / ".failed.json").write_text(
                    json.dumps(
                        {
                            "stage": "memory_extraction",
                            "error": "Unexpected keyword argument for some other call",
                        }
                    )
                )
                unrelated_type_error = self.recovery.audit_archive(archive, **kwargs)
                self.assertEqual(
                    unrelated_type_error["reason"], "failure_requires_manual_review"
                )

                (archive / ".meta.json").unlink()
                missing = self.recovery.audit_archive(archive, **kwargs)
                self.assertEqual(missing["reason"], "missing_meta_or_messages")

    def test_recovery_maintenance_config_is_exactly_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "ov.conf"
            original = (
                '{\n  "memory": {"session_auto_commit": '
                '{"default_enabled": true, "idle_enabled": true}, '
                '"other": 7},\n  "sentinel": "preserve formatting bytes"\n}\n'
            ).encode()
            config_path.write_bytes(original)
            waits = []
            restarts = []
            events = []

            with patch.object(self.recovery, "CONFIG_PATH", config_path), patch.object(
                self.recovery, "BASE", root
            ), patch.object(
                self.recovery, "wait_for_queue_empty",
                side_effect=lambda timeout, all_queues=False: waits.append(
                    (timeout, all_queues)
                ),
            ), patch.object(
                self.recovery, "restart_openviking",
                side_effect=lambda event: restarts.append(event),
            ), patch.object(
                self.recovery, "emit", side_effect=lambda event: events.append(event)
            ):
                with self.assertRaisesRegex(RuntimeError, "canary failed"):
                    with self.recovery.preserve_runtime_config(True):
                        maintenance = json.loads(config_path.read_text())
                        auto_commit = maintenance["memory"]["session_auto_commit"]
                        self.assertFalse(auto_commit["default_enabled"])
                        self.assertFalse(auto_commit["idle_enabled"])
                        raise RuntimeError("canary failed")

            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual(waits, [(1800, True), (1800, True)])
            self.assertEqual(
                restarts,
                ["runtime_maintenance_started", "runtime_restart_after_restore"],
            )
            snapshots = list(root.glob("runtime-before-recovery-*.conf"))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].read_bytes(), original)
            self.assertEqual(events[-1]["event"], "runtime_config_restored")


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
