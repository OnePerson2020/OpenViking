# TTL corner case trial — 2026-10-06

Validation snapshot on top of PR #5268 at `3185348f1e6c`. The final fixed-expiry
scope and later cleanup validation are recorded in
[the October 7 validation report](ttl-no-renewal-validation-20261007.md).

## Changes

1. Session config updates verify that the Session is still live while holding
   the existing Session lock. A request that loaded metadata before expiry or
   deletion cannot recreate it. Legacy Sessions with messages but no metadata
   retain their existing update behavior.
2. TTL application and new owner initialization read current cluster/account
   settings through the existing ConfigSource API. Only TTL models are rebuilt;
   the shared configuration cache, publication and refresh loop keep their
   existing behavior. One account application reuses one resolved policy.
3. Policy application captures each parent's child directory names before
   updating them. Concurrent cleanup cannot shift an unprocessed sibling out
   of an offset page. Updates retain the existing concurrency limit of eight.

These changes add no locks, background jobs, cache invalidation protocol,
generation fields or persistent lifecycle records. The existing TTL merge and
validation helpers handle aliases, null overrides, policy modes and priority.

## Validation

| Check | Result |
| --- | --- |
| Selected native storage, TTL, Session, runtime config and API tests on macOS | 654 passed |
| Same selected tests in a Linux amd64 Kubernetes Pod | 654 passed |
| Documentation theme/navigation tests | 54 passed |
| Ruff on changed Python files; `git diff --check` | Passed |

The 654 cases are the same on both platforms. The selection is the main command
in [the previous validation record](ttl-root-policy-validation-20261005.md#main-suite-command),
with `tests/unit/session/test_event_tag_concurrency.py` also included.

New cases reproduce expiry/deletion during Session configuration, removal of an
earlier sibling while applying TTL to 101 Sessions, and stale configuration in
two managers sharing a source. They also cover legacy metadata, ordinary writes
without new config reads, current/cached policy equivalence and source failures.
Two pre-existing event-tag tests needed their filesystem double to expose
`exists` and accept `include_expired`, matching the actual filesystem interface.

Kubernetes used the existing native extension with the final Python source
overlaid in an isolated Pod and synthetic test configuration. Package source
hashes matched locally and in the Pod. No image or application deployment was
published; the temporary acceptance Pod was deleted.

## Performance

- Ordinary Session append and existing event body writes add zero config source
  reads. New owner initialization adds two reads, issued concurrently.
- Updating one account's policy performs four source reads: a cluster/account
  pair before the patch and a pair for the whole application afterward. The
  number of reads does not grow with its directory count.
- Local native storage with FileConfigSource, 128 Session creations per run,
  eight concurrent creations, five alternating runs: cached baseline median
  batch time **1.9516 s**, final implementation **1.8535 s**. Median individual
  creation times were **68.29 ms** and **69.00 ms**; p95 was **193.64 ms** and
  **190.37 ms**. This sample shows similar local cost, not a throughput guarantee.
- An earlier trial rebuilt the full configuration models and measured about
  20% longer batch time. That full-model reconstruction was removed.
- Listing 10,000 immediate directories, three runs: original offset enumeration
  **2.7228 s / 101 ls calls** versus one listing **0.0305 s / one ls call**.
  Retained names used **0.78 MiB**; measured Python peak was **5.67 MiB**.
  These are enumeration-only measurements, excluding metadata writes and native
  memory. Name storage grows with the number of siblings.

Fresh lifetime initialization now depends on the config source being readable.
Read failures propagate instead of persisting a cached deadline. Remote source
latency, multi-Pod shared storage and production throughput were not measured.
The two-manager test exercises stale caches within one test process. This record
does not claim the entire repository suite or production acceptance is green.

## Source identity

Python package SHA-256:
`4c86ae44baea92328a7796c1b818e08289fb1f605f116d184d0e5a7f36c1447d`.
Calculated over sorted tracked paths under `openviking/` and `openviking_cli/`,
with each path and its bytes separated by NUL bytes.
