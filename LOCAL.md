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

## Deploy

    python3.13 ops/deploy.py            # plan + local_tests on a temp stage (dry run)
    python3.13 ops/deploy.py --apply    # wait idle queue, back up, install, health, rollback on failure

Deploys the committed `local` ref (not the worktree) as an overlay over the installed
upstream wheel and refreshes `upgrade-0423-20261005/port/{merged,files.txt}` + `stage`.
A file whose patch was dropped goes back to the upstream version. Logs + backups:
`local_patches/deploys/<timestamp>/`. A new local fix = a new commit on `local`, then deploy.
A new upstream *version* still needs the wheel built (`upgrade-0423-20261005/build.sh`) and
installed first; deploy.py only manages the overlay files.

## Toolchain

`ops/setup-toolchain.sh` rebuilds `local_patches/toolchain/` (cmake for `build.sh`, pytest for
`improve-20261006/runlocal.sh` / `runut.sh`). Run it after a devbox wipe. Local tests:

    ~/.openviking/local_patches/improve-20261006/runlocal.sh ~/.local/lib/python3.13/site-packages

## Config

`ops/ov.conf.template.json` is the live `~/.openviking/ov.conf` with keys replaced by
`<secret>`. `python3.13 ops/ovconf.py` diffs live against it (exit 1 on drift);
`--write` refreshes it, then commit. Non-default values and why:

- `vlm.max_tokens` 65536, `vlm.timeout` 1800: long extractions were truncated at the 32768
  floor; Ark clamps silently, so 65536 was proven by a real 65536-token generation (784 s).
- `memory.extraction_input_token_budget` 160000 / `extraction_read_token_budget` 16000: WM and
  fallback batch sizes derive from these (local patch).
- `memory.*_format|transport` = `json_schema`: strict extraction (local patch; upstream
  rejects the value, so an unpatched install fails to start).
- `embedding.max_retries` 6 with the bounded embedder (20 s/request): BytePlus SG stalls.
- `queue_workers.session_commit.max_concurrent` 12.
- Restart only on an idle queue (running+pending == 0), as deploy.py does.
