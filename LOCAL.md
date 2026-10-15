# OpenViking local patch series

`local` = our patches, one feature per commit, on top of the upstream release tag
(`v0.5.0` from volcengine/OpenViking; this clone is shallow). Upstream's
README is `README.md`; this file is ours. Fixes worth proposing upstream are
cherry-picked from `local` onto `upstream/main` on the Mac (`~/.openviking/openviking-repo`).
Until 2026-10-09 the series sat on a synthetic sdist import; that history is kept in
branches `sdist-local-0.4.23` / `sdist-upstream-0.4.23`. The last v0.4.23-based series is
branch `local-0.4.23` (2026-10-10).

## Upgrade to a new upstream version

devbox has no direct GitHub access; fetch the tag through the Mac proxy (on the Mac):

    devbox-gh git -C .openviking/local_patches/ov-fork fetch --depth 1 github \
        refs/tags/vX.Y.Z:refs/tags/vX.Y.Z

Then on devbox:

    git branch local-OLD local                       # keep the previous series
    git rebase --onto vX.Y.Z vOLD local             # conflicts are per feature; drop absorbed commits
    ops/build.sh X.Y.Z                              # wheel -> local_patches/build/X.Y.Z/ (~20 min cold)
    python3.13 ops/deploy.py --wheel W.whl          # gate on a stage unpacked from the new wheel
    python3.13 ops/deploy.py --wheel W.whl --apply  # stop, pip install wheel, overlay, start, health

There is no file-level rollback across versions: undo = `deploy.py --apply --wheel <previous
wheel> --ref local-OLD` (the previous wheel path is in `local_patches/deployed-wheel`). Plain
`pip install -U openviking` would silently drop every patch.

## Upgrade notes

- v0.5.0 (2026-10-10): 36 of 38 v0.4.23 commits carried over unchanged. Dropped: the #5591
  backport (in v0.5.0) and "session: Working Memory budget batching, resume and deadline
  retry" (WM is default-off since #5696 and off for our user), replaced by "session:
  long-term extraction falls back to budget batches…"; WM leftovers removed in "drop
  Working Memory leftovers after the v0.5.0 port" (incl. `memory.working_memory_transport`).
- Open upstream PRs (drop the matching local commit once merged, then deploy): #5758
  orphan race, #5759 VLM deadline, #5765 pathlock polling, #5802 archive retry route,
  #5812 JSON image redaction, #5818 C++ log append, #6038 filtered path guard, #6073 inner period in resource-ref
  link anchors (with #6054 merged upstream, drop both resource-ref commits), #6065
  embedding attempt deadline (replaces "embedding: bound Volcengine embedding requests";
  keep `embedding.timeout` 20 in ov.conf when adopting it). Merged upstream, drop on the next upgrade: #6054
  (resource-ref linkify; replaces "memory: resource-ref linkify keeps Markdown line markers out
  of the link"; its follow-up #6069, indented list markers, is still open — our commit already
  handles indentation). Inner "." (`SKILL.md`): fixed here, upstream #6072 / PR #6073. #5759 and #6065 are written
  against upstream #5950 (RetryVLM/RetryEmbedder, after v0.5.0): on an upgrade past it the
  local VLM-deadline and Volcengine-embedding commits conflict; take the PR versions.
- PRs filed 2026-10-15 for local-only behaviour (drop/replace the local part once merged):
  #6075 no upsert from a stale snapshot ("memory: improve extraction robustness" part),
  #6077 reason commit returns once queued ("resources: queue reason commits without waiting"),
  #6080 no .failed.json next to .done (robustness commit part), #6083 lock contention requeues +
  2 s requeue pause ("session: requeue commits on lock contention" + "diagnostics + queue: …pause"),
  #6086 batch large policy-less commits (upstream uses the 150k default; ours uses the input budget),
  #6088 Volcengine query_param instruction ("embedding: Volcengine query_param …").
- Cherry-picked from upstream main (drop on the next upgrade past it): #6039 compact intent
  planner prompt (`intent_analysis.yaml` + 2 tests; recall with query expansion 6-11 s -> 3-5 s,
  2026-10-15).
- Upstream issues our commits work around (drop once fixed upstream): #6006 agent evolution
  reruns per case ("session: train agent evolution once per commit…"), #3526 entity
  duplicates across categories ("memory: route a new entity to the existing card…").
- Client plugins (Claude Code, Codex, pi on the Mac) are upgraded separately from the server.
- Pre-fork work dirs (0.4.23 port, Oct 6–9 improvements) and the only full data backup
  (2026-10-06, before 0.4.23) are in `local_patches/archive/`; wheels in `local_patches/build/`.

## Backup to GitHub

Remote `github` here is volcengine upstream (fetch only). The backup is branch
`devbox/local-<ver>` on OnePerson2020/OpenViking, pushed from the Mac (devbox has no
GitHub credentials):

    cd ~/.openviking/openviking-repo   # remote devbox-fork = devbox:.openviking/local_patches/ov-fork
    git fetch --no-tags devbox-fork local
    git -c http.proxyAuthMethod=basic -c credential.helper= \
        -c credential.helper='!gh auth git-credential' \
        push --force https://github.com/OnePerson2020/OpenViking.git \
        devbox-fork/local:refs/heads/devbox/local-0.5.0

## Deploy

    python3.13 ops/deploy.py            # plan + tests on a temp stage (dry run)
    python3.13 ops/deploy.py --apply    # wait idle queue, back up, install, health, rollback on failure

Deploys the committed `local` ref (not the worktree) as an overlay over the installed
upstream wheel; the live commit is recorded in `local_patches/deployed`, and deploy refuses
to run if live files differ from it.
A file whose patch was dropped goes back to the upstream version. Logs + backups:
`local_patches/deploys/<timestamp>/`. A new local fix = a new commit on `local`, then deploy.
A new upstream *version* still needs the wheel built (`ops/build.sh X.Y.Z`) and
installed first; deploy.py only manages the overlay files.

## Failed archives and daily check

    python3.13 ops/healthcheck.py                                   # read-only; exit 1 = attention
    python3.13 ops/retry_archive.py <session_id> <archive_id>       # hash-bound retry, waits while busy
    python3.13 ops/purge_task.py <backup_dir> [--now] TASK_ID[=SESSION/ARCHIVE] ...

    python3.13 ops/merge_entities.py plan [DIR]  /  apply DIR      # same-name entity cards across categories

`merge_entities.py` merges entity cards whose names match after folding case and -/_/space (the key
new writes are routed by), per space, losslessly: the largest card stays verbatim plus the facts only
the others have; groups the model calls different entities are skipped (`--keys K.. --same` overrides
that for groups the user confirmed). 2026-10-13: 288 groups (94 from a manual review of fuzzy candidates),
349 cards removed; runs in `local_patches/entity-merge/<run>/` (plan, apply log, tar backup).
`plan --similar [--since N]` pairs cards with different names by embedding (>= 0.85, no conflicting
ids — numbers of 3+ digits, dates compared apart — the rule new writes use) and lets the model judge each pair under a strict definition
(a document, event, analysis or variant about X is not X; 2026-10-14: 50 of 96 lenient matches kept).
Weekly cron (Sun 04:00): `plan --similar --since 8` + `apply` into `entity-merge/sweep-<date>/`,
log `~/.openviking/logs/entity-sweep.log`, sweep dirs older than 35 days removed.
    python3.13 ops/split_entity_history.py plan DIR KEEP_URI URI..  /  apply DIR  # oversized cards -> card + events

`split_entity_history.py` turns overlapping oversized cards into one compact card: originals verbatim into one
resource (`viking://resources/entity-archive/...`), dated records into events unless a semantically similar
event already states them (grouped per day/topic), still-true facts into the kept card. 2026-10-13: the 8
LocalBridge cards (329k chars) -> `开发工具/localbridge.md` (7k) + 101 events. Other 40k+ cards (multi_label,
sara, openviking, codex) are candidates.
`purge_task.py` removes failed task records in one restart: session_commit ones only after
their archive has `.done`, bare ids only for non-session tasks (tar backup first; idle queue
unless `--now`). On the Mac, `ops/ov-healthcheck-mac.sh --install
[HH:MM]` installs LaunchAgent `com.openviking.healthcheck` (daily 09:30): runs the check over
ssh, logs to `~/Library/Logs/ov-healthcheck.log`, posts a notification on problems.

## Toolchain

`ops/setup-toolchain.sh` rebuilds `local_patches/toolchain/` (cmake for `ops/build.sh`, pytest
for `ops/test.sh`). Run it after a devbox wipe. Tests (offline, throwaway workspace):

    ops/test.sh ~/.local/lib/python3.13/site-packages                              # local_tests
    TESTS=$PWD ops/test.sh ~/.local/lib/python3.13/site-packages tests/session     # upstream tests

Upstream tests that a patch changes are edited in place under `tests/` (never copied into
`local_tests/`); `deploy.py` runs `local_tests`, every upstream test file that differs from the
release tag, and the files listed in `ops/upstream-tests.txt`.

## Config

`ops/ov.conf.template.json` is the live `~/.openviking/ov.conf` with keys replaced by
`<secret>`, Ark endpoint IDs and internal hosts by `<private>`, the home directory by `~`
(the backup branch is public). `python3.13 ops/ovconf.py` diffs live against it (exit 1 on drift);
`--write` refreshes it, then commit. Non-default values and why:

- `vlm.max_tokens` 40000, `vlm.timeout` 1800: the largest normal output in 19,839 calls was
  30,795 tokens; every `finish_reason=length` call was a runaway that fills any cap (65536 only
  doubled the waste, ~13 min per call). Samples: `logs/truncated-outputs.jsonl`.
- `memory.extraction_input_token_budget` 160000 / `extraction_read_token_budget` 16000: WM and
  fallback batch sizes derive from these (local patch).
- `memory.extraction_output_format` = `json` since 2026-10-14. The local strict `json_schema`
  protocol (provider-constrained output, Ark stop sequences, truncation salvage) was removed
  2026-10-15: A/B json vs json_schema 15/15 each, json dropped no data; strict only saved ~26%
  prompt tokens and cost ~800 Ark-only lines. Last commit with it: `fb22394`.
- `embedding.max_retries` 6 with the bounded embedder (20 s/request): BytePlus SG stalls.
- `embedding.dense.query_param` = vision-embedding instruction prefix on text queries (local
  patch; documents unchanged, no reindex). 90 vague queries / 300-doc pool: MRR .831 -> .906.
- `queue_workers.session_commit.max_concurrent` 12.
- A commit that waits for its predecessor is requeued after a 2 s pause inside the worker
  ("diagnostics + queue: …pause before commit requeue"). Without it, each waiting message
  cycles ~2/s (7-9% of a core, 2026-10-09). Upstream main (b8ec9d8) still requeues instantly.
- Restart only on an idle queue (running+pending == 0), as deploy.py does.
