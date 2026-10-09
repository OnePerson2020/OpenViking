# OpenViking local patch series

`upstream` = pristine PyPI sdist python packages (tag `upstream/<ver>`).
`local` = our patches, one feature per commit, rebased onto each new upstream.
Replaces hand-merging `upgrade-0423-20261005/port/merged`.

## Upgrade to a new upstream version

    # 1. import the new sdist onto the upstream branch
    pip download --no-deps --no-binary :all: openviking==X.Y.Z -d /tmp/ovsd
    tar -xzf /tmp/ovsd/openviking-X.Y.Z.tar.gz -C /tmp/ovsd
    git checkout upstream && git rm -rq openviking openviking_cli
    cp -r /tmp/ovsd/openviking-X.Y.Z/{openviking,openviking_cli} .
    git add -A && git commit -m "openviking X.Y.Z (PyPI sdist, python packages only)"
    git tag upstream/X.Y.Z
    # 2. replay the patches; conflicts are per feature
    git checkout local && git rebase upstream/X.Y.Z
    # 3. drop commits upstream has absorbed (git rebase skips empty ones)

Alternative to step 1: the `github` remote (volcengine/openviking). devbox has no
direct GitHub access, so run git through the Mac proxy with `devbox-gh` (on the Mac):

    devbox-gh git -C .openviking/local_patches/ov-fork fetch --depth 1 github \
        refs/tags/vX.Y.Z:refs/tags/github/vX.Y.Z

For v0.4.23 the sdist python sources equal that tag except the generated
`_version.py` and `web_studio/dist` assets, so `git rebase github/vX.Y.Z` also works
(the build still needs the sdist).

## Next-upgrade notes (upstream main as of 2026-10-08)

- #5696 makes Working Memory **opt-in**: default off unless the user's
  `memory_policy.working_memory.enabled` is true or a commit passes
  `enable_working_memory=true`. Not an ov.conf key; set the policy before
  deploying a version that contains it, or WM archives silently stop.
- `2f1306a` (WM budget batching/resume) builds on upstream `extraction_batch.py`
  (present since 0.4.23); expect conflicts with #5696/#5591 in session.py.
- Upstream has no failed-archive retry route; `621eb8e` is the cleanest
  candidate to propose upstream.

## Backup to GitHub

Remote `github` here is volcengine upstream (fetch only). The backup is branch
`devbox/local-<ver>` on OnePerson2020/OpenViking, pushed from the Mac (devbox has no
GitHub credentials):

    cd ~/.openviking/openviking-repo   # remote devbox-fork = devbox:.openviking/local_patches/ov-fork
    git fetch --no-tags devbox-fork local
    git -c http.proxyAuthMethod=basic -c credential.helper= \
        -c credential.helper='!gh auth git-credential' \
        push --force https://github.com/OnePerson2020/OpenViking.git \
        devbox-fork/local:refs/heads/devbox/local-0.4.23

## Deploy

    python3.13 ops/deploy.py            # plan + local_tests on a temp stage (dry run)
    python3.13 ops/deploy.py --apply    # wait idle queue, back up, install, health, rollback on failure

Deploys the committed `local` ref (not the worktree) as an overlay over the installed
upstream wheel and refreshes `upgrade-0423-20261005/port/{merged,files.txt}` + `stage`.
A file whose patch was dropped goes back to the upstream version. Logs + backups:
`local_patches/deploys/<timestamp>/`. A new local fix = a new commit on `local`, then deploy.
A new upstream *version* still needs the wheel built (`upgrade-0423-20261005/build.sh`) and
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

`ops/setup-toolchain.sh` rebuilds `local_patches/toolchain/` (cmake for `build.sh`, pytest for
`improve-20261006/runlocal.sh` / `runut.sh`). Run it after a devbox wipe. Local tests:

    ~/.openviking/local_patches/improve-20261006/runlocal.sh ~/.local/lib/python3.13/site-packages

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
