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

## Export the overlay for build/deploy (deploy.py reads port/merged + files.txt)

    P=~/.openviking/local_patches/<new-port-dir>/port
    git diff --name-only upstream/X.Y.Z local -- openviking openviking_cli > $P/files.txt
    mkdir -p $P/merged && git archive local $(cat $P/files.txt) | tar -x -C $P/merged

A new local fix = a new commit on `local`, then export as above.
