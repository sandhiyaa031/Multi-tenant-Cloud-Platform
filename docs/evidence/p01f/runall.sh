#!/bin/bash
# P0.1f A/A diagnostic: the two P0.1e levels that showed the L10 stall and the L30 creep, same profiles, no action.
cd "D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform"
export MSYS_NO_PATHCONV=1
P=results/p01f/progress.log
for L in 10 30; do
  rm -f results/p01f/stop_$L
  bash results/p01f/state.sh results/p01f/state_L${L}_start.txt
  bash results/p01f/monitor.sh $L &
  mpid=$!
  bash results/p01f/powerlog.sh $L &
  ppid=$!
  echo "[$(date -u +%FT%TZ)] run L$L start" >> $P
  docker compose run --rm -e PYTHONPATH=/srv --entrypoint python evaluation /results/p01f/run.py $L 420 > results/p01f/console_L$L.log 2>&1
  echo "[$(date -u +%FT%TZ)] run L$L exit=$?" >> $P
  touch results/p01f/stop_$L; wait $mpid; wait $ppid
  bash results/p01f/state.sh results/p01f/state_L${L}_end.txt
  # database-side events of this run (log files are pruned after 15 min, so capture now)
  docker compose exec -T dp-primary sh -c "cat /var/log/dbpilot/pg-*.json 2>/dev/null | grep -h -E '\"message\":\"(checkpoint (starting|complete)|automatic (vacuum|analyze))'" 2>/dev/null | python -c "
import sys,json
for l in sys.stdin:
    try: d=json.loads(l); print(d['timestamp'], d['message'][:220])
    except Exception: pass" > results/p01f/pglog_L$L.txt 2>&1
  echo "[$(date -u +%FT%TZ)] run L$L state and database events captured; resting 90 s" >> $P
  sleep 90
done
echo "[$(date -u +%FT%TZ)] ALL DONE" >> $P
