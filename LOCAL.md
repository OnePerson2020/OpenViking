# OpenViking local patch series

`local` = our patches, one feature per commit, on top of the upstream release tag
(`v0.5.1` from volcengine/OpenViking; this clone is shallow). Upstream's
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

- v0.5.1 (2026-10-20): the series was rebuilt on `v0.5.1` as one commit per feature instead
  of replaying 110 commits (v0.5.0 series kept as branch `local-0.5.0`, last `7788de9b5`).
  Open upstream PRs are carried as their PR commits, so a merged PR drops out cleanly.
  - Dropped, covered by v0.5.1: #6075 stale-snapshot guard, #4548 async PathLock (our
    async_client part), #6054 resource-ref linkify, #6077/#6157 queued reason commit, #6153
    `.done` after the task result (and our `.failed.json`-next-to-`.done` guard), #6039 planner
    prompt, #5950 RetryVLM/RetryEmbedder (our VLM deadline loop and vision retries), #6102 /
    #6047 timeout classification and no query retries, #6136/#6043 task tracking.
  - Dropped, no longer used: every Volcengine embedding patch (bounded requests, Ark
    keep-alive, `query_param` instruction prefix = PR #6088, SDK-timeout retries). Embedding is
    the gpubox Qwen3-Embedding-4B through `provider: openai` since 2026-10-20.
  - Dropped as dead: tolerant QueueFS payload decoding (byte-integer `data` came from the 0.4.23
    Go binding; ragfs serializes `data` as a string). Dropped as redundant: "a commit that failed
    its task is a failed queue result" (an extra task read per commit; healthcheck already reports
    failed tasks and failed archives, the queue error count would only repeat them).
  - Carried as PR commits (drop each once merged): #5758 orphan race, #5759 VLM per-attempt
    deadline, #5802 archive retry (adapted here: `has_work` is async and `MAX_TASKS` is gone in
    v0.5.1; the PR branch needs the same fix), #5812 JSON image redaction, #6038 filtered path
    guard, #6069 indented list markers, #6073 inner period, #6083 lock requeue + 2 s pause, #6086
    fallback batches, #6152 resource tree slot, #6179 write conflicts (merged with #6075: other
    read errors propagate), #6180 deferred session load, #6181 task listing cache (only its
    perf commit; the task-registration commit is covered by #6122, the API test tweaks are CI-only).
  - Local only: VLM call log (`log.model_calls_output`), C++ vectordb log file (PR #5818 fixes
    the sink in C++, which an overlay cannot ship), MinerU error type, queue worker error log,
    session load under the writers' locks, bounded reads + partial-read safety + entity routing
    (#3526; the fallback batch of #6086 is capped at half the input budget so it fits the budget
    check), agent-artifact reason skip, evolution once per commit (#6006), expansion failure log.
  - After the port (2026-10-20): upstream #6301 cherry-picked (retry a memory step whose fresh
    read failed before any write; drop on the next upgrade past it), plus the local "stale patch
    preflight": since #6179 a patch over a file another commit changed after the model read it
    failed the whole archive mid-batch (~10 archives 10-17..10-20, mostly profile.md and hot
    project cards). The conflict is now found before any write and is retryable under #6301's
    rule, so the step regenerates from fresh content. Candidate for an upstream PR on top of #6301.
  - Upstream #6331 cherry-picked (drop on the next upgrade past it): v0.5.1 ran streaming-batch
    flushes under the submitting session_commit's Task context, so vectorization enqueued after the
    commit completed was rejected ("Task ... is unavailable; rejected work for Embedding", issue
    #6322): memories were written without vectors. 143 rejections 2026-10-20 14:48-19:05 here;
    the affected URIs were re-indexed after the fix.
  - PR #6362 (carried; drop once merged): a bare OpenAIError (client configuration, e.g. missing
    credentials) is not retried. It was UNKNOWN, so RetryVLM x Phase 2 step retry made 16 attempts
    (~26 s) per extraction step; session-commit test teardowns went from ~26 s to < 1 s.
  - PR #6355 (tests only, carried; drop once merged): restore os.environ after every test. The
    LiteLLM provider exports OPENAI_API_KEY; leaked from tests/models into test_session_commit.py
    (in the deploy gate since #6301), it turned offline model calls into real requests and hung
    the run at the 1200 s limit four times on 2026-10-20.
  - Entity look-alike threshold recalibrated for Qwen3-Embedding-4B on 2026-10-20: 0.85 -> 0.90
    on the find() score (77% of reviewed duplicates surfaced, 38% of new cards get a note; at
    0.85 it was 80% of new cards). Recalibrate after any embedding model change: data and
    scripts in devbox `/tmp/cal` were throwaway; method in the extract_loop comment.
- Client plugins (Claude Code, Codex, pi on the Mac) are upgraded separately from the server.
- Pre-fork work dirs (0.4.23 port, Oct 6–9 improvements) and the only full data backup
  (2026-10-06, before 0.4.23) are in `local_patches/archive/`; wheels in `local_patches/build/`.
  The v0.5.0-era upgrade notes are in `LOCAL.md` on branch `local-0.5.0`.

## Backup to GitHub

Remote `github` here is volcengine upstream (fetch only). The backup is branch
`devbox/local-<ver>` on OnePerson2020/OpenViking, pushed from the Mac (devbox has no
GitHub credentials):

    cd ~/.openviking/openviking-repo   # remote devbox-fork = devbox:.openviking/local_patches/ov-fork
    git fetch --no-tags devbox-fork local
    git -c http.proxyAuthMethod=basic -c credential.helper= \
        -c credential.helper='!gh auth git-credential' \
        push --force https://github.com/OnePerson2020/OpenViking.git \
        devbox-fork/local:refs/heads/devbox/local-0.5.1

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
`plan --similar [--since N]` pairs cards with different names by embedding (>= 0.90 since Qwen, no conflicting
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
  doubled the waste, ~13 min per call). Samples: `logs/truncated-outputs.jsonl`. Since v0.5.1
  (#5759) the timeout bounds each RetryVLM attempt, not the whole call (local v0.5.0 had one
  deadline over all retries): worst case is (`vlm.max_retries` + 1) x 1800 s.
- `memory.extraction_input_token_budget` 160000 / `extraction_read_token_budget` 16000: every
  extraction call and read is bounded by these; the policy-less fallback batch is half the input
  budget (local patch).
- `memory.extraction_output_format` = `json` since 2026-10-14. The local strict `json_schema`
  protocol (provider-constrained output, Ark stop sequences, truncation salvage) was removed
  2026-10-15: A/B json vs json_schema 15/15 each, json dropped no data; strict only saved ~26%
  prompt tokens and cost ~800 Ark-only lines. Last commit with it: `fb22394`.
- `embedding.dense`: gpubox Qwen3-Embedding-4B, `provider: openai`, `dimension` 1024,
  `query_param: "query"` (the server prepends the Qwen retrieval instruction for queries).
- `queue_workers.session_commit.max_concurrent` 12.
- A commit that waits for its predecessor is requeued after a 2 s pause inside the worker
  ("diagnostics + queue: …pause before commit requeue"). Without it, each waiting message
  cycles ~2/s (7-9% of a core, 2026-10-09). Upstream v0.5.1 still requeues instantly (PR #6083).
- Restart only on an idle queue (running+pending == 0), as deploy.py does.
