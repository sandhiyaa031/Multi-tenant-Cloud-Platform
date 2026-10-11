#!/usr/bin/env bash
# Part B of tools/final/protocol.md: real changes through the real loop, on the development stack.
# usage: tools/final/run_trials.sh        (from the repository root; the stack must be up; resumable)
set -uo pipefail
cd "$(dirname "$0")/../.."
export MSYS_NO_PATHCONV=1
OUT=results/final
mkdir -p "$OUT"
{
  echo "commit $(git rev-parse HEAD)"
  echo "started $(date -u +%FT%TZ)"
  echo "TWIN_MAX_LOOKS=$(docker exec dbpilot-engine-1 printenv TWIN_MAX_LOOKS)"
  grep -E '^(SEED_SCALE|EVAL_RATE_SCALE|EVAL_POOL_SIZE|TWIN_WINDOW_S|TWIN_DELAY_S|TWIN_REPETITIONS|COLLECT_INTERVAL_S)=' .env
} >> "$OUT/trials_meta.txt"
# The harness generates its own workload; the demo workload must not run beside it.
docker compose --profile demo stop demo-load >/dev/null 2>&1
docker compose run --rm evaluation --matrix --reps 3 \
  --scenarios S1_missing_index,S9_no_change \
  --configs C2_no_verification,C3_canary_only,C4_twin_per_tenant \
  --out /results/final/trials.jsonl 2>&1 | tee -a "$OUT/trials_console.log"
echo "finished $(date -u +%FT%TZ)" >> "$OUT/trials_meta.txt"
