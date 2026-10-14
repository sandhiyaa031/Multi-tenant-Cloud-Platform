#!/usr/bin/env bash
# Brings DBPilot up for a demonstration and says when it is ready.
#
#   scripts/review_start.sh            the stack as the development laptop runs it
#   SMALL=1 scripts/review_start.sh    a host with fewer cores (adds docker-compose.small.yml)
#
# Idempotent: run it again at any time. Nothing is exposed beyond this machine: every published port
# is bound to 127.0.0.1 in docker-compose.yml.
set -uo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

COMPOSE=(docker compose)
[ "${SMALL:-0}" = "1" ] && COMPOSE+=(-f docker-compose.yml -f docker-compose.small.yml)
say() { echo "[$(date +%H:%M:%S)] $*"; }
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Python is needed for the readiness check (python3 or python)." >&2; exit 1; }

docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop, wait for it to say it is running, and run this again." >&2; exit 1; }
[ -f .env ] || { echo "There is no .env. Run scripts/make_env.sh to create one with random secrets." >&2; exit 1; }

say "starting the stack (the first start builds images and loads the demo data: about 10 minutes)"
"${COMPOSE[@]}" up -d --build 2>&1 | grep -E "error|Error|failed" ; true
say "waiting for the data plane to be seeded"
for _ in $(seq 1 120); do
  state=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' dbpilot-dp-seed-1 2>/dev/null || echo "missing")
  [ "$state" = "exited 0" ] && break
  [ "${state%% *}" = "exited" ] && { echo "The data-plane seed failed ($state). See: docker compose logs dp-seed" >&2; exit 1; }
  sleep 5
done
say "waiting for the API"
until curl -sf -m 3 http://127.0.0.1:8000/healthz >/dev/null; do sleep 2; done
say "registering the demo organization (safe to repeat)"
"${COMPOSE[@]}" run --rm demo-seed 2>&1 | tail -3
say "starting demo traffic"
"${COMPOSE[@]}" --profile demo up -d demo-load 2>&1 | grep -E "error|Error|failed"; true

say "waiting for telemetry (the console needs two collector windows)"
for _ in $(seq 1 36); do
  "$PY" scripts/demo_admin.py status >/tmp/dbpilot_status.txt 2>&1 && break
  sleep 10
done
cat /tmp/dbpilot_status.txt
echo
echo "Console:  http://localhost:5173   (sign in as the DEMO_ADMIN_EMAIL / DEMO_ADMIN_PASSWORD in .env)"
echo "API docs: http://localhost:8000/docs"
echo "Start at 'Guided demo' in the sidebar. When finished: scripts/review_stop.sh"
