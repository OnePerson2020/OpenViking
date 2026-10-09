#!/bin/bash
# usage: ops/build.sh X.Y.Z
# Builds the upstream wheel from the PyPI sdist for devbox (GLIBC 2.28 -> manylinux_2_28 tag),
# into ~/.openviking/local_patches/build/X.Y.Z/. Local patches are not in the wheel: they are
# deployed on top of it with ops/deploy.py. Needs ops/setup-toolchain.sh (cmake) and ~/.cargo.
set -euo pipefail
V=$1; LP=$HOME/.openviking/local_patches; B=$LP/build/$V
mkdir -p "$B"; cd "$B"
[ -d "openviking-$V" ] || {
  python3.13 -m pip download -q --no-deps --no-binary :all: "openviking==$V" -d .
  tar -xzf "openviking-$V.tar.gz"
}
cd "openviking-$V"
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$LP/toolchain/build-tools/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH=$LP/toolchain/build-tools CARGO_TARGET_DIR=$B/target CARGO_BUILD_JOBS=4
export CARGO_NET_OFFLINE=${CARGO_NET_OFFLINE:-true}  # ponytail: cached crates only; set false if a new version needs new crates
export OPENVIKING_VERSION=$V SETUPTOOLS_SCM_PRETEND_VERSION=$V
nice python3.13 -u -c 'import os,runpy; os.cpu_count=lambda:4; runpy.run_path("setup.py",run_name="__main__")' \
  bdist_wheel --plat-name manylinux_2_28_x86_64 -d "$B" > "$B/build.log" 2>&1
ls "$B"/openviking-"$V"-*.whl
