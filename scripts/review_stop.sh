#!/usr/bin/env bash
# Stops the demonstration. The data is kept, so the next start is fast.
#
#   scripts/review_stop.sh                undo what a demonstration left behind, then stop everything
#   scripts/review_stop.sh --no-reset     just stop
#
# To remove the data as well (the next start then rebuilds and reseeds everything):
#   docker compose --profile demo down -v
set -uo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
PY=$(command -v python3 || command -v python || true)

if [ "${1:-}" != "--no-reset" ] && [ -n "$PY" ] && curl -sf -m 3 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  "$PY" scripts/demo_admin.py reset || echo "The demo could not be fully restored; see the message above." >&2
fi
docker compose --profile demo stop demo-load >/dev/null 2>&1
docker compose --profile demo down
echo "Stopped. Data volumes are kept."
