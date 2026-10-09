#!/bin/bash
# Rebuild ~/.openviking/local_patches/toolchain (used by build.sh, runlocal.sh, runut.sh).
# Idempotent; re-run after a devbox wipe. Was /tmp/openviking-audit.AhRt7j until 2026-10-09.
#   build-tools: cmake for the sdist native build (system cmake too old)
#   testsite:    pytest only; openviking itself comes from <pkg-root> / user site
set -euo pipefail
T=$HOME/.openviking/local_patches/toolchain
mkdir -p "$T"
python3.13 -m pip install -q --target "$T/build-tools" cmake==4.4.3
python3.13 -m pip install -q --target "$T/testsite" pytest==8.4.2 pytest-asyncio==1.4.0
"$T/build-tools/bin/cmake" --version | head -1
PYTHONPATH=$T/testsite python3.13 -m pytest --version
