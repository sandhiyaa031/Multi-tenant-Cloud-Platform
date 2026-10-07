#!/bin/bash
# P0.1h platform soak. Usage: soak.sh <idle_s> <soak_s> <rest_s> <reburn_s>
# Phases: IDLE (canary only) -> SOAK (5 burners + canary) -> REST (canary only) -> REBURN (5 burners + canary).
# Burners use unassigned vCPUs 9,10,11,20,21; the two canaries use 22 and 23. DBPilot is left untouched and idle.
cd "D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform"
export MSYS_NO_PATHCONV=1
D=results/p01h
IDLE=$1; SOAK=$2; REST=$3; REBURN=$4
OUT="D:\\college\\PROJECTS-SEM 5\\dbpilot\\Multi-tenant-Cloud-Platform\\results\\p01h"
M=$D/markers.log
mark() { echo "$(date -u +%FT%TZ) $1" >> $M; }
docker rm -f p01h_burn p01h_canary22 p01h_canary23 >/dev/null 2>&1
rm -f $D/stop_host
mark "START plan idle=$IDLE soak=$SOAK rest=$REST reburn=$REBURN"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$OUT\\hostlog.ps1" -Out "$OUT\\host.jsonl" -Stop "$OUT\\stop_host" -MaxSeconds $((IDLE+SOAK+REST+REBURN+300)) &
HPID=$!
sleep 5
# Canary: fixed work (500,000 shell-loop iterations, ~1.1 s), timed with /proc/uptime in centiseconds; logs elapsed ms.
CAN='while :; do a=$(cut -d" " -f1 /proc/uptime | tr -d .); i=0; while [ $i -lt 500000 ]; do i=$((i+1)); done; b=$(cut -d" " -f1 /proc/uptime | tr -d .); echo "$(date -u +%FT%TZ) $(( (b-a)*10 ))"; sleep 3; done'
docker run -d --name p01h_canary22 --cpuset-cpus=22 -v "$OUT:/out" alpine:3.22 sh -c "$CAN >> /out/canary22.log" >/dev/null
docker run -d --name p01h_canary23 --cpuset-cpus=23 -v "$OUT:/out" alpine:3.22 sh -c "$CAN >> /out/canary23.log" >/dev/null
mark "PHASE idle begins"; sleep $IDLE
burn() { docker run -d --name p01h_burn --cpuset-cpus=9,10,11,20,21 alpine:3.22 sh -c 'for k in 1 2 3 4 5; do (while :; do :; done) & done; wait' >/dev/null; }
burn; mark "PHASE soak begins (5 burners on vCPUs 9,10,11,20,21)"; sleep $SOAK
docker rm -f p01h_burn >/dev/null 2>&1; mark "PHASE rest begins (burners removed)"; sleep $REST
burn; mark "PHASE reburn begins"; sleep $REBURN
docker rm -f p01h_burn >/dev/null 2>&1; mark "PHASE reburn ends (burners removed)"; sleep 20
docker rm -f p01h_canary22 p01h_canary23 >/dev/null 2>&1
touch $D/stop_host; wait $HPID; mark "END"
