#!/bin/bash
# usage: monitor.sh <level> ; stops when results/p01e/stop_<level> exists. Read-only sampling.
cd "D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform"
export MSYS_NO_PATHCONV=1
pw=$(grep ^DATAPLANE_OWNER_PASSWORD .env | cut -d= -f2)
out=results/p01e/monitor_L$1.log
W="select 'WAIT active='||count(*) filter (where state='active')||' lock='||count(*) filter (where state='active' and wait_event_type='Lock')||' lwlock='||count(*) filter (where state='active' and wait_event_type='LWLock')||' io='||count(*) filter (where state='active' and wait_event_type='IO') from pg_stat_activity where backend_type='client backend' and datname='app' and pid<>pg_backend_pid()"
while [ ! -f results/p01e/stop_$1 ]; do
  ts=$(date -u +%FT%TZ)
  st=$(docker stats --no-stream --format '{{.Name}}={{.CPUPerc}}' 2>/dev/null | grep -E "dp-primary-1|pgbouncer-1|evaluation-run|collector-1|engine-1" | tr '\n' ' ')
  wait=$(docker compose exec -T -e PGPASSWORD="$pw" dp-primary psql -h localhost -U postgres -d app -At -c "$W" -c "select pg_sleep(0.2)" -c "$W" -c "select pg_sleep(0.2)" -c "$W" -c "select pg_sleep(0.2)" -c "$W" -c "select pg_sleep(0.2)" -c "$W" 2>&1 | grep WAIT | tr '\n' ' ')
  echo "$ts $st $wait" >> "$out"
done
