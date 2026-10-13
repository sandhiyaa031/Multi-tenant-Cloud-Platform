#!/usr/bin/env bash
# Reproduces the measurements of the report, stage by stage, in benchmark mode.
#
#   scripts/run_benchmark.sh up        bring the stack up in benchmark mode and record its configuration
#   scripts/run_benchmark.sh part0     where a transaction's time goes, per workload variant; adopt one
#   scripts/run_benchmark.sh part1     noise floor in production (A/A), 39 minutes
#   scripts/run_benchmark.sh part2     noise floor on the twin by each procedure (A/A), about 80 minutes
#   scripts/run_benchmark.sh report    copy the tables to docs/evidence/bench_noise
#   scripts/run_benchmark.sh all       everything above, in order
#
# Protocol: docs/evidence/bench_noise/protocol.md. Later phases add their stages here.
# Run from Git Bash on the development laptop. SMOKE=1 runs every stage with very short
# windows into results/bench_smoke to check the plumbing; its numbers mean nothing and the
# pre-run checklist is skipped.
set -uo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

SMOKE="${SMOKE:-0}"
if [ "$SMOKE" = "1" ]; then
  OUT=results/bench_smoke; WARM=20; WINDOW=30; SEGMENT=10; REPS=3; ROUNDS=1
else
  OUT=results/bench_noise; WARM=150; WINDOW=180; SEGMENT=60; REPS=12; ROUNDS=12
fi
PROFILE=profiles/eval.json; RATE=0.5; POOL=8
MAX_HOST_CPU=8        # percent of the whole machine, stack up and no load
MAX_PROCESS_CORES=0.5 # of one core, for any single process outside Docker

dc() { docker compose --env-file .env --env-file bench/bench.env -f docker-compose.yml -f docker-compose.bench.yml "$@"; }
say() { echo "[$(date -u +%H:%M:%S)] $*"; }
ps_run() { powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$1" | tr -d '\r'; }

checklist() {
  [ "$SMOKE" = "1" ] && { say "smoke run: checklist skipped"; return 0; }
  local failed=0
  local status; status=$(ps_run '(Get-CimInstance Win32_Battery).BatteryStatus')
  if [ "$status" = "2" ] || [ -z "$status" ]; then say "check: AC power connected"; else say "FAIL: not on AC power (battery status $status)"; failed=1; fi
  local cpus mem; cpus=$(docker info --format '{{.NCPU}}'); mem=$(docker info --format '{{.MemTotal}}')
  if [ "$cpus" = "24" ] && [ "$mem" -gt 10000000000 ] && [ "$mem" -lt 10800000000 ]; then say "check: Docker has $cpus CPUs and $((mem / 1048576)) MiB"
  else say "FAIL: Docker has $cpus CPUs and $((mem / 1048576)) MiB; the protocol's runs had 24 and about 9940"; failed=1; fi
  local heavy; heavy=$(ps_run "\$a = Get-Process | Select-Object Id, Name, CPU; Start-Sleep -Seconds 30; \$b = Get-Process | Select-Object Id, Name, CPU; foreach (\$p in \$b) { \$q = \$a | Where-Object Id -eq \$p.Id; if (\$q -and \$null -ne \$p.CPU) { \$c = (\$p.CPU - \$q.CPU) / 30; if (\$c -gt $MAX_PROCESS_CORES -and \$p.Name -notmatch 'vmmem|docker|com.docker') { '{0} ({1:N2} cores)' -f \$p.Name, \$c } } }")
  if [ -z "$heavy" ]; then say "check: no process outside Docker used more than $MAX_PROCESS_CORES of a core over 30 s"
  else say "FAIL: busy processes outside Docker: $(echo "$heavy" | tr '\n' ' ')"; failed=1; fi
  local total; total=$(ps_run "[math]::Round(((Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 3 -MaxSamples 10).CounterSamples | Measure-Object CookedValue -Average).Average, 1)")
  if awk "BEGIN { exit !($total <= $MAX_HOST_CPU) }"; then say "check: host CPU ${total}% with the stack idle"
  else say "FAIL: host CPU ${total}% with the stack idle (limit ${MAX_HOST_CPU}%)"; failed=1; fi
  [ "$failed" = "0" ] || { say "pre-run checklist failed; nothing was measured"; exit 3; }
}

host_start() {   # keep the machine awake and log the power source until host_stop
  mkdir -p "$1"; rm -f "$1/stop_host"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/bench_host.ps1 -Out "$1/power.csv" -StopFile "$1/stop_host" &
  HOST_PID=$!; sleep 8
  [ -s "$1/power.csv" ] || { say "FAIL: the host helper did not start, so sleep is not held off"; exit 3; }
  say "check: sleep is held off for the run and the power source is being logged"
}
host_stop() { touch "$1/stop_host"; wait "${HOST_PID:-}" 2>/dev/null; rm -f "$1/stop_host"; }

snapshot() {     # what a later reader needs to know about how this was run
  mkdir -p "$OUT"
  {
    echo "commit $(git rev-parse HEAD)$(git diff --quiet || echo ' (with uncommitted changes)')"
    echo "recorded $(date -u +%FT%TZ)"
    echo "docker $(docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes, kernel {{.KernelVersion}}')"
    echo "--- bench/bench.env"; grep -v '^#' bench/bench.env | grep .
    echo "--- containers (cpuset, memory limit, image)"
    docker inspect -f '{{.Name}} cpus={{.HostConfig.CpusetCpus}} mem={{.HostConfig.Memory}} image={{.Image}}' $(dc ps -q) | sort
    echo "--- pg_settings of the primary that differ from the built-in defaults"
    docker exec dbpilot-dp-primary-1 psql -U postgres -d app -Atc "select name || '=' || setting || coalesce(unit, '') from pg_settings where source not in ('default', 'override') order by name"
  } > "$OUT/environment.txt"
  say "configuration recorded in $OUT/environment.txt"
}

stage_up() {
  dc up -d --build 2>&1 | grep -E "Built|Started|error" | tail -20
  until docker exec dbpilot-dp-primary-1 pg_isready -h 127.0.0.1 -U postgres -d app >/dev/null 2>&1; do sleep 2; done
  until curl -sf -m 3 http://127.0.0.1:8000/healthz >/dev/null; do sleep 2; done
  dc --profile demo stop demo-load >/dev/null 2>&1
  snapshot
}

load() {         # load <service> <variant> <seconds> <directory> [detached]
  local service=$1 variant=$2 seconds=$3 dir=$4
  rm -f "$dir/ops.jsonl" "$dir/intervals.jsonl"
  dc --profile bench run --rm ${5:+-d --name bench_load_bg} "$service" --profile "$PROFILE" --rate-scale "$RATE" --pool-size "$POOL" \
     --variant "$variant" --seed 1 --duration "$seconds" --ops "/$dir/ops.jsonl" --out "/$dir/intervals.jsonl"
}
service_of() { [ "$1" = "v3" ] && echo bench-load-local || echo bench-load; }

stage_part0() {
  checklist; host_start "$OUT/part0"
  for variant in v0 v1 v2 v3; do
    local dir="$OUT/part0/$variant"; mkdir -p "$dir"
    say "part 0: workload variant $variant, $((WARM + WINDOW)) s"
    load "$(service_of $variant)" "$variant" $((WARM + WINDOW)) "$dir" | tail -8
    dc --profile bench run --rm bench-tools /tools/bench/db_share.py "/$dir/ops.jsonl" --variant "$variant" \
       --warmup "$WARM" --window "$WINDOW" --out "/$dir/db_share.json" | tee "$dir/db_share.txt"
  done
  # Adoption: the highest variant that ran clean and that the twin replays with at most 1% errors.
  : > "$OUT/part0/adoption.txt"
  for variant in v3 v2 v1 v0; do
    local dir="$OUT/part0/$variant" errors completed
    errors=$(python -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1], encoding='utf-8') if l.strip()]; print(sum(x['errors']+x['dropped'] for x in r), sum(x['count'] for x in r))" "$dir/intervals.jsonl")
    read -r errors completed <<< "$errors"
    if [ "$errors" -gt $((completed / 100)) ]; then echo "$variant: $errors errors or dropped arrivals of $completed; not adopted" | tee -a "$OUT/part0/adoption.txt"; continue; fi
    say "part 0: can the twin replay $variant?"
    mkdir -p "$dir/replay"; rm -f "$dir/replay/"round_*.json
    load "$(service_of $variant)" "$variant" $((WARM + 400)) "$dir/replay" detached >/dev/null
    sleep $((WARM + 30))
    python tools/bench/twin_aa.py --rounds 1 --modes sequential --out "$dir/replay" | tee -a "$OUT/part0/adoption.txt"
    docker rm -f bench_load_bg >/dev/null 2>&1
    if python -c "import json,sys,glob; r=json.load(open(glob.glob(sys.argv[1]+'/round_*.json')[0], encoding='utf-8')); res=r.get('result') or {}; e=sum(sum(a['errors'].values()) for a in res.get('arms', {}).values()); t=res.get('transactions', 0); print(f'  replay errors {e} of {2*t}'); sys.exit(0 if r['state']=='DONE' and t>0 and e <= 0.01*2*t else 1)" "$dir/replay" | tee -a "$OUT/part0/adoption.txt"; [ "${PIPESTATUS[0]}" = "0" ]; then
      echo "$variant" > "$OUT/adopted_variant"; echo "ADOPTED: $variant" | tee -a "$OUT/part0/adoption.txt"; break
    fi
    echo "$variant: not replayed cleanly by the twin; not adopted" | tee -a "$OUT/part0/adoption.txt"
  done
  host_stop "$OUT/part0"
}

adopted() { cat "$OUT/adopted_variant" 2>/dev/null || { say "no workload variant adopted yet: run part0 first"; exit 1; }; }

stage_part1() {
  local variant; variant=$(adopted); local dir="$OUT/part1"
  checklist; host_start "$dir"
  say "part 1: production A/A with variant $variant, $((WARM + REPS * 3 * SEGMENT + 20)) s"
  load "$(service_of $variant)" "$variant" $((WARM + REPS * 3 * SEGMENT + 20)) "$dir" | tail -8
  host_stop "$dir"
  dc --profile bench run --rm bench-tools /tools/bench/noise_prod.py "/$dir" --reps "$REPS" --warmup "$WARM" --segment "$SEGMENT"
}

stage_part2() {
  local variant; variant=$(adopted); local dir="$OUT/part2"
  checklist; host_start "$dir"
  say "part 2: twin A/A, $ROUNDS rounds of three procedures, production under variant $variant"
  load "$(service_of $variant)" "$variant" 21600 "$dir" detached >/dev/null
  sleep $((WARM + 30))
  python tools/bench/twin_aa.py --rounds "$ROUNDS" --out "$dir"
  docker rm -f bench_load_bg >/dev/null 2>&1
  host_stop "$dir"
  dc --profile bench run --rm bench-tools /tools/bench/noise_twin.py "/$dir"
}

stage_report() {
  local dest=docs/evidence/bench_noise; mkdir -p "$dest"
  [ "$SMOKE" = "1" ] && { say "smoke run: nothing is copied to $dest"; return 0; }
  cp "$OUT/environment.txt" "$dest/" 2>/dev/null
  for v in v0 v1 v2 v3; do cp "$OUT/part0/$v/db_share.txt" "$dest/part0_${v}_db_share.txt" 2>/dev/null; cp "$OUT/part0/$v/db_share.json" "$dest/part0_${v}_db_share.json" 2>/dev/null; done
  cp "$OUT/part0/adoption.txt" "$dest/part0_adoption.txt" 2>/dev/null
  cp "$OUT/part1/noise_prod.txt" "$OUT/part1/noise_prod.json" "$OUT/part1/power.csv" "$dest/" 2>/dev/null && mv "$dest/power.csv" "$dest/part1_power.csv"
  cp "$OUT/part2/noise_twin.txt" "$OUT/part2/noise_twin.json" "$dest/" 2>/dev/null
  cp "$OUT/part2/power.csv" "$dest/part2_power.csv" 2>/dev/null
  say "tables copied to $dest"
}

case "${1:-}" in
  up) stage_up ;;
  part0) stage_part0 ;;
  part1) stage_part1 ;;
  part2) stage_part2 ;;
  report) stage_report ;;
  all) stage_up; stage_part0; stage_part1; stage_part2; stage_report ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
