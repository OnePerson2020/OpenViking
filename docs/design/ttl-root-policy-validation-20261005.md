# TTL root policy validation — 2026-10-05

This revision applies library/type/root policy changes to existing live event
directories and Sessions, including unmanaged history. It also validates queued
Session writes against their original persisted Phase 1 task ID and releases the
common metadata lock before ordinary body I/O.

## Results

| Check | Result |
| --- | --- |
| Selected native storage, TTL, Session, config and HTTP API regressions on macOS | 638 passed |
| The same 638 cases in a Linux amd64 Kubernetes Pod | 638 passed |
| Six existing Phase 2/Session retention scenarios replayed with native storage | 6 passed locally and in Kubernetes |
| Real HTTP requests against the running Kubernetes server | Passed |
| Ruff on changed Python files; `git diff --check` | Passed |

The two 638-case runs execute the same cases; they are not 1,276 distinct tests.
The six retention scenarios use native RAGFS and the real Session implementation
in place of the broken shared mock service fixture described below. Their model
responses remain the existing test doubles.

The selected suite covers:

- First enable, history backfill, extension, shortening, disable/re-enable,
  concrete-root priority, startup recovery, and identical-patch retries after
  metadata/index failures. Relative history without a reliable timestamp reports
  incomplete application; absolute policies use the configured deadline directly.
- Fixed event deadlines, relative Session renewal, absolute Session deadlines,
  original completion-time recovery, and rejection of old commits after Session
  deletion or ID reuse. A fresh Session can import an older calendar date.
- Explicit empty event directories, expiry projection, Session/file reads and
  listings, find/search/grep/glob, and expired-vector filtering.
- Strict removal of bodies, messages, archives, L0/L1/L2 vectors and associated
  Meta; retained parent summaries; busy files, active embeddings, partial vector
  failures, confirmation lag, persistent retries and backlog recovery.

## Real HTTP acceptance

The isolated Pod used the rebuilt Rust extension and the current Python source.
Configuration requests used a ROOT key; data requests used a temporary tenant
admin key. Credentials were read inside the Pod and were not written to logs.

1. Create an event file and Session with TTL disabled.
2. Enable seven-day retention, then change it to 30 days. Both existing owners
   receive deadlines before the configuration response; the event gains exactly
   23 days from its original content time.
3. Set an absolute Session deadline and append a message. The deadline is unchanged.
4. Shorten both owners to a near-future absolute deadline. After expiry, direct
   content/download/stat and Session/context reads return 404. Session listing,
   tree, grep, glob and find exclude the expired content.
5. QueueFS removes both physical owner directories, including `.meta.json`.
   Both cleanup tasks report `completed` with `deleted: true`. Appending to the
   deleted Session returns 404.

For this check, the scan interval was one second and both jitter settings were
zero. Physical deletion was observed one second after the visibility assertions.
This is a smoke result, not a production cleanup latency guarantee. The default
physical cleanup window remains day-scale.

## Performance and limits

The native concurrency case admits eight sibling body writes before allowing any
of their body I/O to finish, both with TTL disabled and enabled. This demonstrates
that the common metadata lock no longer serializes their body I/O. It does not
measure production throughput. Configuration application pages 100 entries at a
time and updates at most eight owners concurrently; its cost grows with the
number of affected directories. Cleanup retains count, byte and time budgets.

Validation uses local filesystem storage and the local persistent vector engine,
including inside Kubernetes. Shared object storage under multiple workers, remote
vector-backend deletion, console/gateway behavior and billing reconciliation were
not load-tested or accepted by these results.

The complete repository suite is not claimed green. Broader Session runs hit the
existing `MockLocalAGFS`/runtime-config fixture error for the missing
`/local/_system/runtime_config/cluster.json`; the same initialization failure
reproduces at the previous PR head `16e9fedf`. Additional extraction/working-memory
expectation failures were also reproduced on that head. They are outside this TTL
revision. Direct Phase 2 test fixtures now persist the task identity required by
the real queued-commit contract.

## Artifact identity

- Python package source SHA-256:
  `721195e22c7708bfbf64c02696b278788f36ad5a11b65edf5e399d4e6822f79b`.
  Calculated over sorted tracked/untracked source paths and file bytes under
  `openviking/` and `openviking_cli/`, separated by NUL bytes.
- Linux RAGFS extension SHA-256:
  `66407f0babe03c545394d8527d3dcd80e955457122ffa66a0195f1ef37885f61`.
- Kubernetes image manifest digest:
  `sha256:f6604f7ad2e9a2b2a04808577cfa29147648d0e3683aede4790a4d0f5fd7df8a`.

The Rust build used the modified checkout sources; the two changed Rust files
were hash-compared against the builder. The six retention scenarios used the
updated test fixtures copied into the acceptance Pod after the main suite; the
application source and native extension were unchanged.

## Main suite command

```sh
python -m pytest \
  tests/agfs/test_directory_ttl.py \
  tests/agfs/test_ttl_policy_application.py \
  tests/agfs/test_ttl_commit_lifetime.py \
  tests/agfs/test_ttl_directory_cleanup.py \
  tests/agfs/test_ttl_lock_handoff.py \
  tests/agfs/test_ttl_cleanup_backlog.py \
  tests/unit/core/test_ttl.py \
  tests/unit/config/test_ttl_config.py \
  tests/config/test_ttl_runtime.py \
  tests/unit/session/test_session_commit_resume.py \
  tests/unit/service/test_ttl_regressions.py \
  tests/storage/test_session_commit_processor_identity.py \
  tests/server/test_document_ttl.py \
  tests/unit/service/test_ttl_cleanup.py \
  tests/unit/service/test_ttl_cleanup_retry.py \
  tests/unit/service/test_ttl_daily_cleanup.py \
  tests/unit/storage/test_ttl_registry.py \
  tests/unit/storage/test_ttl_vector_reads.py \
  tests/config/test_runtime_config.py \
  tests/server/test_admin_api.py \
  -q -o addopts='' --disable-warnings
```
