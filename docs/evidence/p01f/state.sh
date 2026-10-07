#!/bin/bash
# usage: state.sh <file> ; read-only snapshot of the database state (exact partition row counts, dead tuples, sizes)
cd "D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform"
export MSYS_NO_PATHCONV=1
pw=$(grep ^DATAPLANE_OWNER_PASSWORD .env | cut -d= -f2)
{
echo "TIME $(date -u +%FT%TZ)"
for t in order_line orders history; do
  for r in t_steady t_bursty t_analytic t_mixed; do
    n=$(docker compose exec -T -e PGPASSWORD="$pw" dp-primary psql -h localhost -U postgres -d app -At -c "select count(*) from ch.${t}_${r}")
    echo "ROWS ${t}_${r} $n"
  done
done
docker compose exec -T -e PGPASSWORD="$pw" dp-primary psql -h localhost -U postgres -d app -At -F ' ' -c "select 'DEAD', relname, n_dead_tup, pg_total_relation_size(relid) from pg_stat_user_tables where schemaname='ch' and relname ~ '^(warehouse|district|stock_|customer_|order_line_|orders_|history_|new_order_)' order by relname" -c "select 'SETTING per_gather', current_setting('max_parallel_workers_per_gather')" -c "select 'DB size', pg_database_size('app')"
} > "$1"
