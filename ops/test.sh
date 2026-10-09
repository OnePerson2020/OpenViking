#!/bin/bash
# usage: ops/test.sh <pkg-root-containing-openviking> [pytest args...]
# Runs local_tests (or TESTS=<dir>, e.g. TESTS=<fork> ... tests/session for upstream tests)
# against <pkg-root>, offline, in a throwaway workspace. Exit code = pytest's.
set -o pipefail
S=$(realpath "$1"); shift
FORK=$(cd "$(dirname "$0")/.." && pwd)
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
sed "s#@W@#$W#g" "$FORK/ops/test-offline.conf" > "$W/ov.conf"
cd "${TESTS:-$FORK/local_tests}" || exit 2
export PYTHONPATH=$S:$PWD:$HOME/.local/lib/python3.13/site-packages:$HOME/.openviking/local_patches/toolchain/testsite
export OPENVIKING_CONFIG_FILE=$W/ov.conf PYTHONNOUSERSITE=1 LITELLM_LOCAL_MODEL_COST_MAP=true
export HOME=$W/home; mkdir -p "$HOME"
timeout 1200 python3.13 -m pytest -q -p no:cacheprovider -o addopts= -rfE "$@" 2>&1 | tail -25
