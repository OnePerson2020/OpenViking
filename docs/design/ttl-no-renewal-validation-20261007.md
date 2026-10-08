# Directory TTL scope and validation — 2026-10-07

## Scope

- Library, type and exact events/sessions root policies determine directory
  deadlines. First enablement and policy changes update existing live owners
  through the same directory metadata path, retaining their original business time.
- Event dates and Sessions own `expires_at`. Ordinary writes and commit replay
  never renew it; expired/deleted objects are not revived by policy changes.
- All content reads enforce expiry and expose the owner deadline. Directory
  counts use backend totals and converge after physical deletion.
- QueueFS cleanup deletes the complete owner directory, its L0/L1/L2, vectors
  and Meta under file locks. It verifies removal and retries incomplete work.
  Parent summaries are unchanged. Old commits cannot mutate a reused Session ID.

## Earlier scope alignment

The earlier pass removed 287 net lines of source and tests (50 added, 337
removed), without adding test files:

- Restored extraction `Context`, MemoryUpdater, summary formatting and semantic
  write callbacks to the main-branch versions. TTL is read from owner metadata;
  similarly named fields in body content do not configure a deadline.
- Removed the unused vector-level deletion parameters. Cleanup deletes every
  level belonging to the owner.
- Removed the first-write journal and duplicate metadata finalization. Both
  stored the same initial deadline after renewal was deferred. Registration
  precedes the body write; a failed body write rolls it back.
- Removed full vector scans for exact live counts. This implements the accepted
  display/statistics delay while keeping content visibility immediate.
- Removed tests of obsolete propagation/stripping and merged repeated scheduling
  boundary checks. Existing integration coverage verifies directory authority.

That pass still kept the durable expiry index and restore reconciliation.
The later directory-scan implementation below removes both while retaining
strict deletion and late-commit/vector guards.

## Earlier scope validation

| Check | Local macOS | Isolated Linux amd64 Kubernetes Pod |
| --- | --- | --- |
| 40-file regression selection | 1,321 passed, 1 skipped | 1,321 passed, 1 skipped |
| Existing native Session integration scenarios | 6 passed | 6 passed |

The selection includes existing configuration, filesystem, Session, extraction,
summary, vector and task-tracker tests; it is not a count of new TTL tests.
The skip is the existing `test_show_blob_raw_returns_envelope`, which needs a
git-enabled fixture. No LLM calls were needed for the six Session scenarios.

Coverage includes policy precedence/application, fixed deadlines, public expiry
visibility, candidate refill, old commits after ID reuse, exact-lock contention,
strict filesystem/vector deletion, partial failures and recovery after restart.
The native backlog case drains 205 expired directories while retaining live
content and parent summaries.

A native-storage before/after probe counted logical writes on first Event body
creation: owner metadata **2 → 1**, registry/index **5 → 3**, body **1 → 1**.
This measures write calls, not production throughput. Remote configuration
latency and multi-Pod load remain unmeasured; the two configuration-source reads
on new-object creation are unchanged.

Ruff, formatting, `git diff --check`, documentation consistency and API-reference
checks passed. The temporary Pod was deleted and its absence verified. This run
copied the final application source into the existing image/native extension;
it did not publish a new image or deploy the service.

## Evidence

Artifacts, the exact test selection, before/after probe and logs:
`/tmp/ov-ttl-scope-alignment-20261007/`.

- Application source SHA-256:
  `b37861bc3d05e1eddd8542a4fdd051e809d6a3b1ce34f032dc3b63c42b5fae2f`.
- 1,688 application/test files matched the local checkout in Kubernetes.
- Native RAGFS SHA-256:
  `66407f0babe03c545394d8527d3dcd80e955457122ffa66a0195f1ef37885f61`.
- Existing image digest:
  `sha256:f6604f7ad2e9a2b2a04808577cfa29147648d0e3683aede4790a4d0f5fd7df8a`.

This is the earlier validation snapshot above `3185348f1e6c`. The final
directory-scan results are recorded below; publication status is tracked in
[PR #5268](https://github.com/volcengine/OpenViking/pull/5268).

## CLI and implementation review

The Rust CLI has `ov ttl get URI` and generic admin configuration get/patch
commands for cluster and account settings. Root policies use that same PATCH
entry point. The CLI does not accept per-object TTL edits. This review removed
duplicate add-resource argument parsing and corrected the TTL help description.
It also added the missing expiry display to Session details, ls/tree tables, and
the existing `--fields` selector; JSON continues to preserve null deadlines.
All 502 CLI tests passed with `RUST_MIN_STACK=16777216`; the default test-thread
stack overflowed in an existing help-rendering test. Eleven localhost checks
verified the actual executable's HTTP requests, table output and nullable JSON.
The relevant cleanup regression selection passed all 30 cases. Ruff and
`git diff --check` passed; Rust files retain pre-existing rustfmt differences.

Web Studio is in this repository, but this PR has no TTL page changes. Its
filesystem and Session TypeScript types also lack `expires_at`. It would need
library/root configuration and read-only expiry display; API filtering already
hides expired content on refresh. The cloud console's code ownership is not
established by this checkout. Neither UI is claimed as delivered.

## Directory-scan implementation

The current code uses paced owner directory discovery and existing QueueFS
workers. It removed per-object due records, claims, retry scheduling, and
write/copy/move/restore index reconciliation. The coarse account marker remains
so accounts that never enabled TTL can be skipped. `.meta.json` is the expiry
source; legacy Event `.ttl.json` remains readable.

Each batch inspects at most 100 owner directories by default, checks a time
budget between owners, and stops admitting work while the queue is busy. The
in-memory iterator finishes the pass before waiting a day. A restarted process
rediscovers expired metadata. Scan cost is O(owner directories in accounts that
have used TTL); listing latency and queued work can extend a pass. No persisted
cursor, tombstone, generation, or replacement scheduler was added.

Deletion first removes the account/URI vector subtree and confirms it is empty
under the existing vector lock. It then removes body files under exact locks,
checks for residue, and removes owner metadata last. Vector errors, silent
vector residue, file errors and silently retained files preserve expiry for the
next scan. Failure of the final directory confirmation still reports an error;
if deletion actually finished, nothing remains to rediscover.

Vectors require no `expires_at` field. Native tests assert its absence from the
collection schema and stored records, then remove owner L0/L1/L2 and orphan
child vectors whose files are missing. A 205-directory backlog test injects six
silent vector failures and verifies rediscovery after scheduler restart. Parent
summaries and live data survive. Historical vector-only orphans whose directory
metadata is already lost cannot be discovered by directory scanning; a known
owner URI can still be cleaned through strict deletion.

The obsolete scheduling/projection tests were removed. Restore guards now check
persisted metadata directly and reject removing managed Event metadata as well
as Session metadata, so raw partial restore cannot detach surviving body files
from expiry. Existing policy, visibility, stale work and lock tests remain.

Validation of this pass: the targeted deletion selection passed **21** checks;
the affected regression selection passed **956**, with **1 skipped**, locally. A further **424** existing compatibility
checks passed (1,380 distinct local checks passed in total).
This selection includes existing tests and is not the number of added TTL tests.
Evidence: `/tmp/ov-ttl-no-schedule-20261007/`.

The same 33-file selection in an isolated Linux amd64 Kubernetes Pod passed
**953**, skipped **1**, and failed **3** existing time-decay score assertions
(expected 0.5, observed 1.0). All TTL deletion checks passed. Running those three
cases against both the pre-simplification source snapshot and current source in
the same Pod reproduced the identical failures. They are outside this cleanup
change; the full Kubernetes selection is not green. Six native Session
integration scenarios also passed. Both temporary Pods were deleted and their
absence verified.

The Kubernetes source package matched **1,687 files** from the working tree.
Application SHA-256 (rechecked before submission on October 8):
`dcd00a21782854ee49ebd233606372058d47f217316b1080b940d1f93e4ba441`.
It used the existing image's native libraries, without publishing a new image.
Remote vector/object-store consistency and multi-process load remain untested.
The only later test-file change removes an unused registry mock from
`test_content_write_processing_mode.py`; that version passed in the local
424-case compatibility selection. Application source is unchanged.

Tracked diff against `c89688ed5bcd`, excluding documentation:

| Category | Before this pass | Current |
| --- | --- | --- |
| Source, including inline Rust tests | +4,606 / -623 | +3,913 / -612 |
| Python tests | +5,627 / -196 | +4,887 / -196 |

This pass removes **1,422 net lines** from the working tree. The remaining diff
still includes the TTL policies, public expiry display/filtering, strict delete,
and lifecycle concurrency guards. Independent review is still pending.
No new image or production deployment is claimed.

## Strict deletion contract

Keep the existing `VikingFS.rm(..., strict=True)` contract during simplification.
TTL cleanup already enables it. Backend deletion errors, remaining vectors, or
failed confirmation must fail the cleanup attempt; only confirmed removal of
files and vectors counts as success. A later cleanup pass can retry.

The public filesystem DELETE route currently uses the default `strict=False`;
it does not expose a strict query parameter. Backend exceptions propagate even
in that mode, but a backend that reports success while leaving vectors behind
is not detected. Native fault injection reproduced both behaviors and verified
that strict mode rejects residual vectors and count failures. All 15 checks
passed, including the existing orphan-vector and cleanup-retry cases. This
check added no production code or permanent test cases. Probe evidence is in
`/tmp/ov-ttl-strict-contract-20261007/`.

The current directory scan uses the deletion ordering above: confirm vectors
and body removal before discarding the owner deadline. Remote backend
consistency and real multi-process load remain outside this evidence.
