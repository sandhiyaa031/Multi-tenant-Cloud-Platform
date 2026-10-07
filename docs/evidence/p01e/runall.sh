#!/bin/bash
# P0.1e: four no-action characterization runs, ascending analytic load, one at a time.
cd "D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform"
export MSYS_NO_PATHCONV=1
P=results/p01e/progress.log
for L in 10 20 30 40; do
  rm -f results/p01e/stop_$L
  bash results/p01e/state.sh results/p01e/state_L${L}_start.txt
  bash results/p01e/monitor.sh $L &
  mpid=$!
  echo "[$(date -u +%FT%TZ)] run L$L start" >> $P
  docker compose run --rm -e PYTHONPATH=/srv --entrypoint python evaluation /results/p01e/run.py $L 420 > results/p01e/console_L$L.log 2>&1
  echo "[$(date -u +%FT%TZ)] run L$L exit=$?" >> $P
  touch results/p01e/stop_$L; wait $mpid
  bash results/p01e/state.sh results/p01e/state_L${L}_end.txt
  echo "[$(date -u +%FT%TZ)] run L$L state captured; resting 90 s" >> $P
  sleep 90
done
echo "[$(date -u +%FT%TZ)] ALL DONE" >> $P
