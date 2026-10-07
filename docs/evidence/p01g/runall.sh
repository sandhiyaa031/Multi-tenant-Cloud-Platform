#!/bin/bash
# P0.1g: full L10-L40 characterization under AC + Best performance, same profiles, no action.
cd "D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform"
export MSYS_NO_PATHCONV=1
P=results/p01g/progress.log
for L in 10 20 30 40; do
  rm -f results/p01g/stop_$L
  bash results/p01g/state.sh results/p01g/state_L${L}_start.txt
  bash results/p01g/monitor.sh $L &
  mpid=$!
  bash results/p01g/powerlog.sh $L &
  ppid=$!
  echo "[$(date -u +%FT%TZ)] run L$L start" >> $P
  docker compose run --rm -e PYTHONPATH=/srv --entrypoint python evaluation /results/p01g/run.py $L 420 > results/p01g/console_L$L.log 2>&1
  echo "[$(date -u +%FT%TZ)] run L$L exit=$?" >> $P
  touch results/p01g/stop_$L; wait $mpid; wait $ppid
  bash results/p01g/state.sh results/p01g/state_L${L}_end.txt
  # database-side events of this run (log files are pruned after 15 min, so capture now)
  docker compose exec -T dp-primary sh -c "cat /var/log/dbpilot/pg-*.json 2>/dev/null | grep -h -E '\"message\":\"(checkpoint (starting|complete)|automatic (vacuum|analyze))'" 2>/dev/null | python -c "
import sys,json
for l in sys.stdin:
    try: d=json.loads(l); print(d['timestamp'], d['message'][:220])
    except Exception: pass" > results/p01g/pglog_L$L.txt 2>&1
  echo "[$(date -u +%FT%TZ)] run L$L state and database events captured; resting 90 s" >> $P
  sleep 90
done
echo "[$(date -u +%FT%TZ)] ALL DONE" >> $P
