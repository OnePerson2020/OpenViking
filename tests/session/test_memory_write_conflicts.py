"""Conflicting memory updates must be visible and must not resurrect deleted cards."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation, ResolvedOperations
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import (
    MemoryUpdateError,
    MemoryUpdater,
    StalePatchError,
)
from openviking.session.memory.merge_op.base import SearchReplaceBlock, StrPatch
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking_cli.exceptions import ConflictError, NotFoundError


class MemoryFS:
    def __init__(self, files):
        self.files = dict(files)
        self.writes = []

    async def read_file(self, uri, **kwargs):
        if uri not in self.files:
            raise NotFoundError(uri, "file")
        return self.files[uri]

    async def write_file(self, uri, content, **kwargs):
        self.writes.append(uri)
        self.files[uri] = content


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["changed", "deleted"])
async def test_conflict_does_not_write_or_report_success(state):
    uri = "viking://user/alice/memories/profile.md"
    old = MemoryFile(uri=uri, content="original-first-version", memory_type="profile")
    current = MemoryFile(uri=uri, content="completely-unrelated-new-fact", memory_type="profile")
    fs = MemoryFS({uri: MemoryFileUtils.write(current)} if state == "changed" else {})
    before = dict(fs.files)
    updater = MemoryUpdater(registry=MemoryTypeRegistry(), vikingdb=None)
    updater._get_viking_fs = lambda: fs
    updater._sync_resource_refs_for_result = AsyncMock()
    updater._vectorize_memories = AsyncMock()
    updater.generate_overview = AsyncMock()
    operation = ResolvedOperation(
        old_memory_file_content=old,
        memory_type="profile",
        uris=[uri],
        memory_fields={
            "content": StrPatch(
                blocks=[
                    SearchReplaceBlock(
                        search=old.content,
                        replace="intended-second-update",
                    )
                ]
            )
        },
    )
    result = await updater.apply_operations(
        ResolvedOperations(upsert_operations=[operation], delete_file_contents=[], errors=[]),
        SimpleNamespace(),
    )
    assert fs.files == before
    assert fs.writes == []
    assert result.edited_uris == result.written_uris == []
    assert len(result.errors) == 1
    assert isinstance(result.errors[0][1], ConflictError)


@pytest.mark.asyncio
async def test_new_card_and_patch_to_unchanged_text_still_work():
    uri = "viking://user/alice/memories/profile.md"
    fs = MemoryFS({})
    updater = MemoryUpdater(registry=MemoryTypeRegistry(), vikingdb=None)
    updater._get_viking_fs = lambda: fs
    operation = ResolvedOperation(
        memory_type="profile", uris=[uri], memory_fields={"content": "first"}
    )
    await updater._apply_upsert(operation, SimpleNamespace())
    operation.old_memory_file_content = MemoryFileUtils.read(fs.files[uri], uri=uri)
    fs.files[uri] = MemoryFileUtils.write(
        MemoryFile(uri=uri, content="first\nunrelated new fact", memory_type="profile")
    )
    operation.memory_fields = {
        "content": StrPatch(blocks=[SearchReplaceBlock(search="first", replace="updated")])
    }
    await updater._apply_upsert(operation, SimpleNamespace())
    assert (
        MemoryFileUtils.read(fs.files[uri], uri=uri).plain_content()
        == "updated\nunrelated new fact"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_stale_patch_cannot_partially_apply(as_dict):
    uri = "viking://user/alice/memories/profile.md"
    old = MemoryFile(uri=uri, content="keep\nold target", memory_type="profile")
    current = MemoryFile(uri=uri, content="keep\nreplacement fact", memory_type="profile")
    original = MemoryFileUtils.write(current)
    fs = MemoryFS({uri: original})
    updater = MemoryUpdater(registry=MemoryTypeRegistry(), vikingdb=None)
    updater._get_viking_fs = lambda: fs
    patch = StrPatch(
        blocks=[
            SearchReplaceBlock(search="keep", replace="changed"),
            SearchReplaceBlock(search="old target", replace="new target"),
        ]
    )
    operation = ResolvedOperation(
        old_memory_file_content=old,
        memory_type="profile",
        uris=[uri],
        memory_fields={"content": patch.model_dump() if as_dict else patch},
    )
    with pytest.raises(ConflictError):
        await updater._apply_upsert(operation, SimpleNamespace())
    assert fs.files[uri] == original
    assert fs.writes == []


@pytest.mark.asyncio
async def test_strict_streaming_submit_propagates_apply_conflict(monkeypatch):
    from openviking.session.memory import streaming_memory_updater as module
    from tests.session.memory.test_streaming_memory_updater import InMemoryVikingFS, _ctx, _registry

    uri = "viking://user/u/memories/cases/example.md"
    fs = InMemoryVikingFS({})
    monkeypatch.setattr(module, "get_viking_fs", lambda: fs)
    monkeypatch.setattr("openviking.session.memory.memory_updater.get_viking_fs", lambda: fs)
    updater = module.StreamingMemoryUpdater(
        registry=_registry(),
        config=module.StreamingMemoryUpdaterConfig(max_operations_per_update=1),
    )
    operation = ResolvedOperation(
        memory_type="cases",
        uris=[uri],
        old_memory_file_content=MemoryFile(uri=uri, content="old", memory_type="cases"),
        memory_fields={"content": "changed"},
    )
    operations = ResolvedOperations(
        upsert_operations=[operation], delete_file_contents=[], errors=[]
    )
    monkeypatch.setattr(MemoryUpdater, "generate_overview", AsyncMock())
    monkeypatch.setattr(MemoryUpdater, "_vectorize_memories", AsyncMock())
    try:
        with pytest.raises(ConflictError, match="no longer exists"):
            await updater.submit(
                module.MemoryUpdateRequest(
                    operations=operations,
                    messages=[],
                    ctx=_ctx(),
                    strict_extract_errors=True,
                )
            )
        assert fs.writes == []
    finally:
        await updater.close()


@pytest.mark.asyncio
async def test_strict_trajectory_extraction_propagates_apply_conflict(monkeypatch):
    from openviking.session.memory.memory_updater import MemoryUpdateResult
    from openviking.session.train.components import trajectory_analyzer as module
    from tests.session.train.test_trajectory_analyzer_component import (
        FakeExtractLoop,
        FakeVikingFS,
        _patch_runtime_defaults,
        _rollout,
    )

    _patch_runtime_defaults(monkeypatch)
    monkeypatch.setattr(module, "ExtractLoop", FakeExtractLoop)
    result = MemoryUpdateResult()
    conflict = ConflictError("Memory field update could not be applied")
    result.add_error("viking://user/u/memories/trajectories/task.md", conflict)
    monkeypatch.setattr(
        module.TrajectoryRolloutAnalyzer,
        "_apply_trajectory_operations",
        AsyncMock(return_value=result),
    )
    resolver = SimpleNamespace(get_vlm=AsyncMock(return_value=SimpleNamespace(model="fake")))
    analyzer = module.TrajectoryRolloutAnalyzer(viking_fs=FakeVikingFS(), vlm_resolver=resolver)
    context = module.TrajectoryAnalyzerContext(
        request_context=SimpleNamespace(
            account_id="default", user=SimpleNamespace(account_id="default", user_id="u")
        ),
        strict_extract_errors=True,
    )
    with pytest.raises(ConflictError) as raised:
        await analyzer.analyze(_rollout(), context)
    assert raised.value is conflict


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", ["experiences", "skills"])
async def test_strict_training_does_not_report_failed_apply_as_submitted(monkeypatch, failed_step):
    from openviking.session import compressor_v3 as module
    from openviking.session.train.domain import (
        PolicyApplyResult,
        PolicySet,
        PolicyUpdatePlan,
        RolloutAnalysis,
    )
    from tests.session.test_compressor_v3 import _ctx, _messages, _training_case

    policies = PolicySet(root_uri="viking://user/u/memories/experiences", policies=[])

    def trainer(step):
        return SimpleNamespace(
            policy_set=policies,
            submit_gradients=AsyncMock(
                return_value=SimpleNamespace(
                    plan=PolicyUpdatePlan(items=[]),
                    apply_result=PolicyApplyResult(
                        updated_policy_set=policies,
                        errors=["write conflict"] if step == failed_step else [],
                    ),
                )
            ),
        )

    gradient = SimpleNamespace(metadata={"memory_type": "skills"})
    analyzer = SimpleNamespace(
        analyze=AsyncMock(
            return_value=RolloutAnalysis(
                evaluation=None,
                trajectories=[],
                gradients=[],
            )
        )
    )
    if failed_step == "skills":
        analyzer.analyze.return_value.gradients = [gradient]
    compressor = module.SessionCompressorV3(vikingdb=None, rollout_analyzer=analyzer)
    compressor._session_skill_extraction_enabled = lambda: failed_step == "skills"
    compressor._get_session_skill_trainer = AsyncMock(return_value=trainer("skills"))
    compressor._link_case_to_training_outputs = AsyncMock()
    monkeypatch.setattr(
        module, "get_viking_fs", lambda: SimpleNamespace(ls=AsyncMock(return_value=[]))
    )
    monkeypatch.setattr(
        module, "get_streaming_policy_trainer", AsyncMock(return_value=trainer("experiences"))
    )
    monkeypatch.setattr(
        module.ExperienceGradientEstimator,
        "estimate",
        AsyncMock(return_value=[object()] if failed_step == "experiences" else []),
    )
    with pytest.raises(RuntimeError, match="write conflict"):
        await compressor.train_from_extracted_cases(
            cases=[_training_case()],
            messages=_messages(),
            ctx=_ctx(),
            strict_extract_errors=True,
        )


@pytest.mark.asyncio
async def test_stale_patch_is_found_before_any_write_and_is_retryable():
    # A concurrent commit changed profile.md after the model read it. The batch also
    # creates a new card: that must not be written, so the step can be regenerated.
    profile = "viking://user/alice/memories/profile.md"
    card = "viking://user/alice/memories/entities/tools/new_tool.md"
    old = MemoryFile(uri=profile, content="works on alpha", memory_type="profile")
    current = MemoryFile(uri=profile, content="works on beta", memory_type="profile")
    fs = MemoryFS({profile: MemoryFileUtils.write(current)})
    updater = MemoryUpdater(registry=MemoryTypeRegistry(), vikingdb=None)
    updater._get_viking_fs = lambda: fs
    updater._sync_resource_refs_for_result = AsyncMock()
    updater._vectorize_memories = AsyncMock()
    updater.generate_overview = AsyncMock()
    new_card = ResolvedOperation(
        memory_type="entities", uris=[card], memory_fields={"name": "new_tool", "content": "x"}
    )
    stale_edit = ResolvedOperation(
        old_memory_file_content=old,
        memory_type="profile",
        uris=[profile],
        memory_fields={
            "content": StrPatch(blocks=[SearchReplaceBlock(search="alpha", replace="gamma")])
        },
    )
    result = await updater.apply_operations(
        ResolvedOperations(
            upsert_operations=[new_card, stale_edit], delete_file_contents=[], errors=[]
        ),
        SimpleNamespace(),
    )
    assert fs.writes == []
    assert [uri for uri, _ in result.errors] == [profile]
    assert isinstance(result.errors[0][1], StalePatchError)
    assert MemoryUpdateError(result).retryable


@pytest.mark.asyncio
async def test_patch_over_changed_text_that_still_matches_is_applied():
    uri = "viking://user/alice/memories/profile.md"
    old = MemoryFile(uri=uri, content="works on alpha", memory_type="profile")
    current = MemoryFile(uri=uri, content="works on alpha\nlikes tea", memory_type="profile")
    fs = MemoryFS({uri: MemoryFileUtils.write(current)})
    updater = MemoryUpdater(registry=MemoryTypeRegistry(), vikingdb=None)
    updater._get_viking_fs = lambda: fs
    updater._sync_resource_refs_for_result = AsyncMock()
    updater._vectorize_memories = AsyncMock()
    updater.generate_overview = AsyncMock()
    edit = ResolvedOperation(
        old_memory_file_content=old,
        memory_type="profile",
        uris=[uri],
        memory_fields={
            "content": StrPatch(blocks=[SearchReplaceBlock(search="alpha", replace="gamma")])
        },
    )
    result = await updater.apply_operations(
        ResolvedOperations(upsert_operations=[edit], delete_file_contents=[], errors=[]),
        SimpleNamespace(),
    )
    assert result.errors == []
    assert "works on gamma" in MemoryFileUtils.read(fs.files[uri], uri=uri).plain_content()
    assert "likes tea" in MemoryFileUtils.read(fs.files[uri], uri=uri).plain_content()


@pytest.mark.asyncio
async def test_stale_patch_in_a_shared_flush_fails_only_its_own_request(monkeypatch):
    # Two commits flush together. The stale patch preflight rejects the whole batch, but
    # the commit that creates an unrelated card must still be written, not report success
    # with nothing written. The stale one fails with a retryable MemoryUpdateError.
    import asyncio

    from openviking.session.memory import streaming_memory_updater as module
    from tests.session.memory.test_streaming_memory_updater import (
        InMemoryVikingFS,
        _ctx,
        _note_op,
        _registry,
    )

    stale_uri = "viking://user/u/memories/notes/hot.md"
    old = MemoryFile(
        uri=stale_uri,
        content="works on alpha",
        memory_type="notes",
        extra_fields={"note_name": "hot"},
    )
    current = MemoryFile(
        uri=stale_uri,
        content="works on beta",
        memory_type="notes",
        extra_fields={"note_name": "hot"},
    )
    fs = InMemoryVikingFS({stale_uri: MemoryFileUtils.write(current)})
    for name in ("streaming_memory_updater", "memory_updater"):
        monkeypatch.setattr(f"openviking.session.memory.{name}.get_viking_fs", lambda: fs)
    monkeypatch.setattr(MemoryUpdater, "generate_overview", AsyncMock())
    monkeypatch.setattr(MemoryUpdater, "_vectorize_memories", AsyncMock())
    stale_edit = ResolvedOperation(
        old_memory_file_content=old,
        memory_type="notes",
        uris=[stale_uri],
        memory_fields={
            "note_name": "hot",
            "content": StrPatch(blocks=[SearchReplaceBlock(search="alpha", replace="gamma")]),
        },
    )
    new_note = _note_op("fresh")
    requests = [
        module.MemoryUpdateRequest(
            operations=ResolvedOperations(
                upsert_operations=[op], delete_file_contents=[], errors=[]
            ),
            messages=[],
            ctx=_ctx(),
            strict_extract_errors=True,
            metadata={"session_id": f"s{index}", "source_extraction_id": f"e{index}"},
        )
        for index, op in enumerate([stale_edit, new_note])
    ]
    updater = module.StreamingMemoryUpdater(
        registry=_registry(),
        config=module.StreamingMemoryUpdaterConfig(max_operations_per_update=2),
    )
    try:
        stale_result, fresh_result = await asyncio.gather(
            *(updater.submit(request) for request in requests), return_exceptions=True
        )
    finally:
        await updater.close()
    assert isinstance(stale_result, MemoryUpdateError), stale_result
    assert stale_result.retryable
    assert not isinstance(fresh_result, BaseException), fresh_result
    assert fresh_result.apply_result.written_uris == [new_note.uris[0]]
    assert new_note.uris[0] in fs.files
    assert MemoryFileUtils.read(fs.files[stale_uri]).plain_content() == "works on beta"


@pytest.mark.asyncio
async def test_strict_trajectory_extraction_raises_retryable_error_for_stale_patch(monkeypatch):
    from openviking.session.memory.memory_updater import MemoryUpdateResult
    from openviking.session.train.components import trajectory_analyzer as module
    from tests.session.train.test_trajectory_analyzer_component import (
        FakeExtractLoop,
        FakeVikingFS,
        _patch_runtime_defaults,
        _rollout,
    )

    _patch_runtime_defaults(monkeypatch)
    monkeypatch.setattr(module, "ExtractLoop", FakeExtractLoop)
    uri = "viking://user/u/memories/trajectories/task.md"
    result = MemoryUpdateResult()
    result.add_error(uri, StalePatchError(uri, "content", "stale patch must still match"))
    monkeypatch.setattr(
        module.TrajectoryRolloutAnalyzer,
        "_apply_trajectory_operations",
        AsyncMock(return_value=result),
    )
    resolver = SimpleNamespace(get_vlm=AsyncMock(return_value=SimpleNamespace(model="fake")))
    analyzer = module.TrajectoryRolloutAnalyzer(viking_fs=FakeVikingFS(), vlm_resolver=resolver)
    context = module.TrajectoryAnalyzerContext(
        request_context=SimpleNamespace(
            account_id="default", user=SimpleNamespace(account_id="default", user_id="u")
        ),
        strict_extract_errors=True,
    )
    with pytest.raises(MemoryUpdateError) as raised:
        await analyzer.analyze(_rollout(), context)
    assert raised.value.retryable
