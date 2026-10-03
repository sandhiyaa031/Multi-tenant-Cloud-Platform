# DBPilot Learning Guide

For each component: what it is, why DBPilot uses it, how it works, where it sits
in DBPilot, and how to explain it to a professor. Sections marked **Built** point
at code you can read and run today. Sections marked **Designed** describe the
frozen design in [ARCHITECTURE.md](ARCHITECTURE.md); they will gain code
references as each milestone lands.

---

## 1. PostgreSQL internals used — Built (control plane)

**What.** PostgreSQL is a relational DBMS. DBPilot uses it twice: as the system
being optimised (data plane) and as its own store (control-plane database).

**Why.** It exposes what an optimiser needs — statistics views, JSON execution
plans, hypothetical indexes, per-role settings — and it has row-level security.

**How — the pieces in the control-plane schema:**

| Concept | Where | What to notice |
|---|---|---|
| Normalisation, M:N | [001_foundation.sql](../controlplane/migrations/001_foundation.sql) | `memberships` resolves users ↔ organizations and carries the role; nothing is duplicated |
| Composite foreign key | same, `tenants → clusters (id, org_id)` | a tenant cannot reference another organization's cluster, by construction |
| Exclusion constraint | same, `tenants_no_overlapping_ranges` | "no two ranges overlap" is not expressible with UNIQUE; a GiST exclusion constraint checks it |
| Generated column | same, `users.has_password` | lets the API know "has a password" without being able to read the hash |
| Partial index | same, `password_reset_tokens_user_idx` | indexes only unused tokens |
| Triggers | [002_audit.sql](../controlplane/migrations/002_audit.sql) | audit rows are written by the database, not by application code that could forget |
| Row lock for a race | same, `guard_last_admin` | `SELECT … FOR UPDATE` on the organization row serialises two concurrent demotions |
| SECURITY DEFINER functions | [004_auth_functions.sql](../controlplane/migrations/004_auth_functions.sql) | narrow doorways for operations the API role is otherwise forbidden to do |
| Transactions | `cp.signup` | organization, user, membership and audit row commit together or not at all |

**Explain it.** "The schema enforces its own invariants. Overlapping tenant
ranges, cross-organization references, a rewritten audit log and an organization
with no admin are all impossible at the database level, and there is a test for
each."

## 2. Row-level security (RLS) — Built

**What.** A per-row filter PostgreSQL adds to every query on a table, defined by
policies.

**Why.** Organization isolation should not depend on every query remembering a
`WHERE org_id = …`.

**How.** The API logs in as `dbpilot_api`, which is not the table owner and has
`NOBYPASSRLS`. Each request opens a transaction and calls
`set_config('app.user_id', …, true)` and `set_config('app.org_id', …, true)`
([db.py](../controlplane/app/db.py)). The `true` makes them transaction-local, so
a pooled connection cannot carry identity into the next request. Policies in
[003_security.sql](../controlplane/migrations/003_security.sql) require the row
to be in that organization **and** the caller to hold a minimum role there
(`cp.has_role`).

**On the data plane** ([02_schema.sql](../dataplane/postgres/initdb/02_schema.sql)).
Tenants share tables. Each tenant logs in as its own role and owns a range of
warehouse ids recorded in `ch.tenant_map`. The policy is
`w_id >= ch.my_w_lo() AND w_id <= ch.my_w_hi()`. Those functions are `STABLE`,
so the planner evaluates them once and prunes the other tenants' partitions:
`EXPLAIN` shows `Subplans Removed: 3`. Tenants hold no privileges on the
partitions themselves, so the policy cannot be sidestepped.

**In DBPilot.** RBAC is enforced twice: `require_role` in
[deps.py](../controlplane/app/deps.py), then the policy. The data plane will use
the same idea with one database role per tenant.

**Explain it.** "With no context set, the API's own database role sees zero rows.
A VIEWER issuing a raw INSERT is refused by PostgreSQL, not by my Python.
`tests/test_authorization.py` connects as that role and proves both."

## 3. Authentication and RBAC — Built

**What.** Argon2id password hashes, signed JWT access tokens, three roles:
VIEWER < OPERATOR < ADMIN.

**How.** The token carries only user and organization ids
([security.py](../controlplane/app/security.py)). The role is read from the
database on every request, so a demotion applies to tokens already issued. Reset
and invite tokens are random, single-use, and only their SHA-256 is stored. Login
and forgot-password answer identically for known and unknown emails.

**Explain it.** "A stolen database dump gives no usable passwords or reset
tokens, and removing a member locks them out on their next request."

## 4. Query optimisation and EXPLAIN — Designed

**What.** The planner picks a plan (scan types, join order, join methods) using
table statistics and cost settings. `EXPLAIN` shows the chosen plan;
`EXPLAIN (ANALYZE, BUFFERS)` runs it and shows actual rows, time and I/O.

**Why.** Most optimisations DBPilot can propose work by changing the plan.

**How to read a plan.** Bottom-up. Compare estimated and actual rows: a large gap
means stale statistics. A sequential scan with a selective filter suggests a
missing index. "Sort Method: external merge" means `work_mem` was too small.

**In DBPilot.** Plans are an agent observation; tier T1 compares plans with and
without a hypothetical index using HypoPG, which costs milliseconds.

**Explain it.** "T1 asks the planner what it would do; T2 measures what actually
happens. T1 is cheap and sometimes wrong, which is why T2 exists."

## 5. pg_stat_statements and the telemetry collector — Built

**What.** An extension that aggregates statistics per normalised query
("fingerprint": literals replaced by `$1`), per database role.

**Why.** It answers "which queries cost the most, for whom" cheaply.

**How.** Counters only grow, so the collector snapshots them every minute and
subtracts. Because each tenant has its own role, every row is already per-tenant.
It gives means, not percentiles; percentiles come from the sampled statement log.

**The collector** ([collector.py](../controlplane/app/collector.py)) snapshots the
view every 60 s and stores `current − previous` in `cp.query_stats`
([005_telemetry.sql](../controlplane/migrations/005_telemetry.sql)), a table
partitioned by day. Three details worth knowing:

- A counter that went *down* means the statistics were reset; the current value
  is then the whole delta.
- A fingerprint missing from the previous snapshot first ran inside the window,
  so it counts from zero. (A test caught the first version dropping these.)
- A fingerprint is the *shape* of the parse tree. `WHERE id = 1` and
  `WHERE id = 2` are one fingerprint; so are two queries differing only in a
  column alias.

It logs in to the data plane as `dbpilot_monitor` (member of `pg_monitor`: can
read statistics, cannot read tables) and to the control plane as
`dbpilot_collector` (can append telemetry, cannot read users or the audit log).

**Explain it.** "One role per tenant turns a standard extension into per-tenant
telemetry with no proxy and no query tagging. The collector stores deltas, so a
row means what a tenant did in one minute, and it runs with only the privileges
that job needs."

## 6. PgBouncer — Built (pooling and tenant-only login); caps and routing actions Designed

**What.** A connection pooler between clients and PostgreSQL.

**Why.** PostgreSQL has no per-tenant resource groups. The pooler is the control
point for per-tenant concurrency caps (A7), routing a tenant's reads to the
replica (A6), and exposing a setting to only a fraction of sessions (canary).

**How.** [entrypoint.sh](../dataplane/pgbouncer/entrypoint.sh) writes the config.
Tenant roles are created at runtime, so PgBouncer looks up each one's SCRAM
verifier through `pgbouncer.get_auth`, a function that answers only for tenant
roles: the owner cannot be reached through the pooler. `app` points at the
primary and `app_ro` at the replica. Pooling is per transaction.

**Explain it.** "It is the one place where DBPilot can limit or redirect a tenant
without touching the application."

## 7. Agentic AI and tool calling — Designed

**What.** An LLM that is given functions it may call, decides which to call,
reads the results and continues.

**Why.** Diagnosis means combining plans, waits, statistics and history; that is
where a language model helps.

**How in DBPilot.** Tools are read-only (top queries, plan, table profile, wait
profile, SLO status, what-if index, history). The agent's only output with effect
is a JSON action matching the schema of A0–A8. A deterministic executor compiles
it to SQL. The agent cannot approve its own proposal; verification is separate
code. A rule-based proposer implements the same interface, which gives the
baseline and lets everything run without an API key.

**Explain it.** "The model chooses among bounded actions; it never writes SQL
that executes. If it hallucinates, the worst case is a rejected proposal."

## 8. Digital twin — Designed

**What.** A real PostgreSQL instance cloned from production, used to measure a
change before production sees it.

**How.** A delayed standby stays ~15 minutes behind production. On a proposal it
is copied twice: control and treatment. The action is applied to treatment only;
the last 15 minutes of captured workload are replayed against both; per-tenant
differences go to the verification engine.

**Explain it.** "It is not a simulation. It is the same data, the same
statistics and the same queries. And cloning for verification is not my
invention — Azure SQL does it for indexes; my contribution is the per-tenant
gate and measuring what the twin is worth."

## 9a. Workload driver — Built

**What.** A load generator ([workload/](../workload/)) that plays four tenants
against the data plane: the five TPC-C transactions
([tpcc.py](../workload/workload/tpcc.py)) and seven analytical queries
([olap.py](../workload/workload/olap.py)).

**Why our own.** Standard benchmark tools connect as one user and cannot confine
a client to one tenant's warehouses. We need one role per tenant, per-tenant
rates, bursts, and per-tenant measurements.

**How — open loop.** Arrival times are drawn from a Poisson process
([driver.py](../workload/workload/driver.py)) and do not wait for earlier
requests to finish. Latency is measured from the *scheduled* arrival, so time
spent waiting for a connection counts. A closed-loop generator (send, wait,
send) slows down when the database is slow and so never records the worst
latencies; this is called coordinated omission, and it was one of the flaws in
the earlier prototype's measurements.

**Explain it.** "The driver behaves like independent users, not like a script
waiting politely. Its measurements are the ground truth for evaluation and are
kept separate from what DBPilot observes about itself."

## 9b. Workload replay — Designed

**What.** Re-executing captured production statements, with their parameters,
timing and sessions, against another instance.

**Why.** Only the real mix shows cross-tenant effects.

**Limits to state openly.** Concurrent writes do not replay deterministically;
logging every statement has overhead; so replay error rate and capture overhead
are measured and reported.

## 10. Verification and statistics — Designed

**What.** A decision rule over measured differences.

**Key ideas.**
- *Confidence interval*: the range the true effect plausibly lies in.
- *Superiority* for the target tenant: the whole interval shows improvement.
- *Non-inferiority* for every other tenant: the whole interval lies below the
  allowed regression. "Not significantly worse" is not the same thing and is not
  accepted.
- *Inconclusive* is its own outcome, never treated as safe.

**Explain it.** "A change is approved only if I can show it helps the target and
can show it does not hurt each of the others."

## 11. Canary deployment and rollback — Designed

**What.** Applying a change to a small part of production first and watching.

**How.** What "small part" means depends on the action: one tenant's partition
for an index, a fraction of sessions for a role setting, the replica first for a
read-path instance setting. The canary enforces the rollback contract derived
from the twin. Rollback runs the action's stored inverse and fires on contract
breach, error spikes, lag, deviation from prediction, lost telemetry, or timeout.

**Explain it.** "The twin predicts; the canary checks the prediction against
reality on limited scope; rollback is automatic and each action has a defined
inverse."

## 12. Docker and Compose — Built

**What.** A container packages a process with its dependencies; Compose starts
several containers as one system.

**In DBPilot.** [docker-compose.yml](../docker-compose.yml) runs the
control-plane database, a one-shot migration job and the API. `depends_on` with
health conditions gives the order: database healthy → migrations done → API.

**Explain it.** "One command reproduces the environment, including the database
version and extensions."

## 13. Kubernetes (k3s) — Designed

**Why here.** Three planes on three nodes; a taint on the twin node guarantees
replay load never lands next to production. That is the specific value;
Kubernetes is not used where Compose suffices.

## 14. Observability — Designed

Prometheus scrapes host and container metrics. Database-level telemetry is
collected by DBPilot itself into the control-plane database, because it must be
per-tenant and joined with proposals.

## 15. Cloud architecture — Designed

Control plane, data plane and experimentation plane, as in
[ARCHITECTURE.md](ARCHITECTURE.md) §3. The separation is what lets the system
experiment without distorting what it measures.

---

## Try it yourself

```bash
docker compose up -d --build
docker compose run --rm dp-seed                                # load the four tenants (~5 min)
docker compose run --rm --no-deps api python -m pytest -q     # 51 control-plane tests
docker compose run --rm dp-test                                # 18 data-plane tests
docker compose run --rm demo-seed                              # register the tenants as a demo organization
docker compose run --rm workload --duration 180                # generate load; the collector records it
docker compose exec control-db psql -U dbpilot_owner -d dbpilot_control
```

In `psql`, connect as the API role and see RLS for yourself:

```sql
SET ROLE dbpilot_api;
SELECT count(*) FROM cp.tenants;            -- 0: no context
SELECT password_hash FROM cp.users;         -- permission denied
```

API documentation is served at http://localhost:8000/docs.

See a tenant's view of the data plane, and partition pruning under RLS:

```bash
docker compose exec dp-primary psql "postgresql://t_steady:<DATAPLANE_TENANT_PASSWORD>@127.0.0.1/app"
```

```sql
SELECT w_id FROM ch.warehouse;                       -- only warehouses 1 and 2
EXPLAIN (COSTS OFF) SELECT count(*) FROM ch.orders;  -- "Subplans Removed: 3"
```
