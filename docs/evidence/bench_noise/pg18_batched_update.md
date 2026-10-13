# A PostgreSQL 18.6 crash found while batching New-Order (2026-10-13)

Not a result of the project. It is recorded because it changed the workload design, and because it looks like a
server bug that could be reported upstream. It was found during plumbing checks, before any Phase 1 measurement.

## What happened

To cut New-Order's round trips, its stock updates were first written as one statement that locks the rows in a fixed
order and then updates them:

```sql
WITH l AS (SELECT * FROM unnest($1::int[], $2::int[], $3::int[]) AS l(i_id, supply_w, qty)),
     held AS (SELECT s.s_w_id, s.s_i_id FROM ch.stock s JOIN l ON s.s_w_id = l.supply_w AND s.s_i_id = l.i_id
              ORDER BY s.s_i_id, s.s_w_id FOR UPDATE OF s)
UPDATE ch.stock s SET s_quantity = ..., s_ytd = s.s_ytd + l.qty, s_order_cnt = s.s_order_cnt + 1, ...
FROM l, held
WHERE s.s_w_id = l.supply_w AND s.s_i_id = l.i_id AND held.s_w_id = s.s_w_id AND held.s_i_id = s.s_i_id
RETURNING s.s_i_id, s.s_dist_04
```

`ch.stock` is range-partitioned by warehouse (one partition per tenant) and has row-level security; the statement was
sent as a protocol-level prepared statement through PgBouncer by tenant roles.

- It passed the workload tests and ran about 1,500 times at the benchmark's light load.
- Under concurrent orders it crashed client backends: `client backend (PID 98) was terminated by signal 11:
  Segmentation fault`, with this statement named as what the process was running. Every crash restarts all sessions.
- In a stress run (New-Order only, 140 orders per second over three tenants, `tools/bench/profiles/stress_new_order.json`) it crashed five times in 90 seconds, about every 18 seconds.
- After the last crash one backend running the statement stayed `active` with no wait event for more than 20 minutes,
  holding its row locks. `pg_terminate_backend` returned true and the backend did not exit; the primary had to be
  restarted. While it lived, every New-Order of that tenant timed out.
- Server: PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2), extensions pg_stat_statements 1.12, auto_explain, hypopg 1.4.3.

A plain `UPDATE ch.stock s SET ... FROM unnest(...) AS l WHERE s.s_w_id = l.supply_w AND s.s_i_id = l.i_id RETURNING ...`
did not crash in 66,000 orders at the same rate, but in that clean run it once returned fewer rows than the order had
lines. That was seen as a missing key in the driver, not investigated further, and is not claimed to be a server fault.

## What was done about it

The batched New-Order (`new_order_batched` in `workload/workload/tpcc.py`) reads its items in one statement and
inserts its lines in one, and updates stock one row per statement by primary key, as the original does. In the same
stress run that form completed 59,710 orders with no error, no crash, no deadlock and no stuck backend.

The driver now counts an operation that fails in its own code as an error, so nothing can drop out of a measurement
unseen.

## What is not known

The cause. No core file was kept and nothing was bisected: whether it needs the partitioning, the row-level security,
the generic plan of a prepared statement, or only concurrent updates of the same rows under a CTE with `FOR UPDATE`.
A reproduction outside this stack would be the next step before reporting it.
