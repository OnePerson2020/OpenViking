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
`pip install -U openviking` would silently drop every patch, and the stock package refuses
`extraction_output_format: json_schema`, so it would not even start.

## Upgrade notes

- v0.5.0 (2026-10-10): 36 of 38 v0.4.23 commits carried over unchanged. Dropped: the #5591
  backport (in v0.5.0) and "session: Working Memory budget batching, resume and deadline
  retry" (WM is default-off since #5696 and off for our user), replaced by "session:
  long-term extraction falls back to budget batches…"; WM leftovers removed in "drop
  Working Memory leftovers after the v0.5.0 port" (incl. `memory.working_memory_transport`).
- Open upstream PRs (drop the matching local commit once merged, then deploy): #5758
  orphan race, #5759 VLM deadline, #5765 pathlock polling, #5802 archive retry route,
  #5812 JSON image redaction, #5818 C++ log append.
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
    python3.13 ops/purge_task.py <backup_dir> <task_id> <session_id> <archive_id>

`purge_task.py` only removes the old failed task record after the archive has `.done`
(tar backup first, idle-queue restart). On the Mac, `ops/ov-healthcheck-mac.sh --install
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
`<secret>`. `python3.13 ops/ovconf.py` diffs live against it (exit 1 on drift);
`--write` refreshes it, then commit. Non-default values and why:

- `vlm.max_tokens` 40000, `vlm.timeout` 1800: the largest normal output in 19,839 calls was
  30,795 tokens; every `finish_reason=length` call was a runaway that fills any cap (65536 only
  doubled the waste, ~13 min per call). Samples: `logs/truncated-outputs.jsonl`.
- `memory.extraction_input_token_budget` 160000 / `extraction_read_token_budget` 16000: WM and
  fallback batch sizes derive from these (local patch).
- `memory.*_format|transport` = `json_schema`: strict extraction (local patch; upstream
  rejects the value, so an unpatched install fails to start).
- `embedding.max_retries` 6 with the bounded embedder (20 s/request): BytePlus SG stalls.
- `queue_workers.session_commit.max_concurrent` 12.
- Restart only on an idle queue (running+pending == 0), as deploy.py does.
