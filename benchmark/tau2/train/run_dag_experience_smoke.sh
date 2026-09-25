#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
SLOT="${DAG_TAU2_SLOT:-7}"
TASK_INDEX="${DAG_TAU2_TASK_INDEX:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slot) SLOT="$2"; shift 2 ;;
    --task-index) TASK_INDEX="$2"; shift 2 ;;
    -h|--help)
      cat <<'USAGE'
Usage: bash benchmark/tau2/train/run_dag_experience_smoke.sh [--slot N] [--task-index N]

Runs one train task and one same-task evaluation with DAG experience learning enabled.
It uses an isolated nonzero slot; default slot is 7 and default task index is 0.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if ! [[ "${SLOT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "--slot must be a nonzero integer for an isolated smoke run" >&2
  exit 2
fi
if ! [[ "${TASK_INDEX}" =~ ^[0-9]+$ ]]; then
  echo "--task-index must be a non-negative integer" >&2
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "PYTHON_BIN is not executable: ${PYTHON_BIN}" >&2
  exit 2
fi

export PYTHON_BIN
export TAU2_MAX_ROLLOUT_CONCURRENCY=1
export TAU2_ROLLOUT_THREAD_WORKERS=1

exec "${SCRIPT_DIR}/restart_vikingbot_train_eval.sh" \
  --slot "${SLOT}" \
  --enable-agent-evolution \
  --epochs 1 \
  --train-index "${TASK_INDEX}" \
  --eval-split train \
  --eval-index "${TASK_INDEX}" \
  --trials 1 \
  --train-trials 1 \
  --concurrency 1 \
  --commit-concurrency 1 \
  --skip-baseline-eval \
  --skip-final-eval \
  --no-clean-result
