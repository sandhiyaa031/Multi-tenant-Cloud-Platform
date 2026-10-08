#!/usr/bin/env bash
# A/A validation of the pair-based gate on a Linux host. See tools/aa/protocol.md.
# usage: tools/aa/run_linux.sh [n_runs]        (run from the repository root; needs docker compose and python3)
set -euo pipefail
N=${1:-16}
OUT=results/aa_linux
cd "$(dirname "$0")/../.."
mkdir -p "$OUT"

if [ "$(nproc)" -lt 20 ]; then echo "this host has $(nproc) CPUs; the Compose file pins CPUs 0-19" >&2; exit 1; fi

if [ ! -f .env ]; then
  secret() { python3 -c "import secrets; print(secrets.token_urlsafe(32))"; }
  {
    for name in CONTROL_DB_OWNER_PASSWORD CONTROL_DB_API_PASSWORD CONTROL_DB_COLLECTOR_PASSWORD CONTROL_DB_ENGINE_PASSWORD \
                JWT_SECRET DATAPLANE_OWNER_PASSWORD DATAPLANE_REPLICATION_PASSWORD DATAPLANE_PGBOUNCER_AUTH_PASSWORD \
                DATAPLANE_MONITOR_PASSWORD DATAPLANE_EXECUTOR_PASSWORD DATAPLANE_TENANT_PASSWORD TWIN_TOKEN DEMO_ADMIN_PASSWORD; do
      echo "$name=$(secret)"
    done
    cat <<'EOF'
PUBLIC_WEB_URL=http://localhost:5173
CORS_ORIGINS=http://localhost:5173
DEMO_ADMIN_EMAIL=admin@demo.dbpilot.dev
COLLECT_INTERVAL_S=20
TWIN_DELAY_S=60
TWIN_WINDOW_S=55
SEED_SCALE=0.3
SEED_ITEMS=30000
TWIN_REPETITIONS=1
EVAL_RATE_SCALE=0.5
EVAL_POOL_SIZE=8
LOG_RETENTION_MIN=15
DP_SHARED_BUFFERS=512MB
STANDBY_SHARED_BUFFERS=128MB
DP_MEM_LIMIT=1536m
REPLICA_MEM_LIMIT=768m
TWIN_MEM_LIMIT=1536m
EOF
  } > .env
  chmod 600 .env
fi

{
  echo "commit $(git rev-parse HEAD)"; uname -a; nproc; lscpu | grep -E 'Model name|Thread|Core|Socket|MHz|Hypervisor' || true
  free -m | head -2; echo "load average before: $(cat /proc/loadavg)"; date -u +%FT%TZ
} > "$OUT/host.txt"
sha256sum tools/aa/protocol.md tools/aa/aa_analyze.py tools/aa/aa_run.py tools/aa/aa_load.py core/dbpilot_core/gate.py > "$OUT/hashes.txt"

docker compose up -d --build
docker compose run --rm dp-seed
docker compose run --rm demo-seed
sleep 30   # replica and twin source catch up with the seeded data

docker rm -f aa_load >/dev/null 2>&1 || true
docker compose run -d --name aa_load --rm -e PYTHONPATH=/srv -v "$PWD/tools:/tools:ro" --entrypoint python \
  evaluation /tools/aa/aa_load.py $((N * 150 + 600)) /results/aa_linux
python3 tools/aa/aa_run.py "$N" 150 "$OUT"
docker rm -f aa_load >/dev/null 2>&1 || true
echo "load average after: $(cat /proc/loadavg)" >> "$OUT/host.txt"

docker compose run --rm --no-deps -v "$PWD/tools:/tools:ro" -v "$PWD/results:/results" api \
  python /tools/aa/aa_analyze.py /results/aa_linux | tee "$OUT/analysis.txt"
