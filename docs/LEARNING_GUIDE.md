# DBPilot Learning Guide

For each component: what it is, why DBPilot uses it, how it works, where it sits
in DBPilot, and how to explain it to a professor. Each section says whether the
part is built and how far it has been verified; where something was written but
not yet run for real, it says so.

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

## 4. Query optimisation and EXPLAIN — Built

**What.** The planner picks a plan (scan types, join order, join methods) using
table statistics and cost settings. `EXPLAIN` shows the chosen plan;
`EXPLAIN (ANALYZE, BUFFERS)` runs it and shows actual rows, time and I/O.

**Why.** Most optimisations DBPilot can propose work by changing the plan.

**How to read a plan.** Bottom-up. Compare estimated and actual rows: a large gap
means stale statistics. A sequential scan with a selective filter suggests a
missing index. "Sort Method: external merge" means `work_mem` was too small.

**In DBPilot.**
- The Query Intelligence page shows the plan of any fingerprint. It is produced
  with `EXPLAIN (GENERIC_PLAN)`, which can plan `$1`-style text without
  parameter values, on the twin source and never on production
  ([whatif.py](../twin/agent/whatif.py)).
- Tier T1 registers a hypothetical index with HypoPG and compares planner cost
  with and without it. No index is built; it takes milliseconds.

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

## 6. PgBouncer — Built

**What.** A connection pooler between clients and PostgreSQL.

**Why.** It is the only door tenants use, which makes it the place to limit or
redirect one tenant without touching the application.

**How.** [entrypoint.sh](../dataplane/pgbouncer/entrypoint.sh) writes the config.
Tenant roles are created at runtime, so PgBouncer looks up each one's SCRAM
verifier through `pgbouncer.get_auth`, a function that answers only for tenant
roles: the owner cannot be reached through the pooler. `app` points at the
primary and `app_ro` at the replica. Pooling is per transaction. After a
role-level change the engine issues `RECONNECT` on the admin console so server
connections are recycled and pick up the new setting.

**Limits to state.** The concurrency cap (A7) is applied with
`ALTER ROLE … CONNECTION LIMIT` rather than a pooler setting. Replica routing
(A6) is defined as an action but is advisory only: it cannot be reproduced on a
single twin instance, so it is never auto-approved.

**Explain it.** "It is the one place where DBPilot can limit or redirect a tenant
without touching the application."

## 7. Agentic AI and tool calling — Built (not yet run against a live model)

**What.** An LLM that is given functions it may call, decides which to call,
reads the results and continues until it has an answer.

**Why.** Diagnosis means combining SLO status, expensive queries, plans, table
profiles and history; that is where a language model helps.

**How** ([agent.py](../controlplane/app/proposers/agent.py)).
- Ten read-only tools over the [Observer](../controlplane/app/observe.py): SLO
  status, latency, top queries, tenant load, plan of a query, table profile,
  current settings, what-if index, history, tenant list.
- One tool with an effect: `propose_action`. Its input is validated against the
  typed action space ([actions.py](../core/dbpilot_core/actions.py)). Invalid
  input is returned to the model as an error so it can correct itself; it never
  reaches a database.
- The loop is bounded (14 turns). A refusal, a cut-off response, or an agent
  that ends without proposing all become "no action, escalate to a human".
- Every tool call and result is stored with the proposal, and shown in the Agent
  Console, so a reviewer sees what the agent looked at.
- A rule-based proposer ([rules.py](../controlplane/app/proposers/rules.py))
  implements the same interface: it is the baseline, and it lets the whole
  pipeline run without an API key.

**What is and is not verified.** The loop is tested against a scripted model
(tool use, error correction, refusal, turn limit). It has not yet been run
against the live API in this project, because no API key was available.

**Explain it.** "The model chooses among bounded actions; it never writes SQL
that executes, and it cannot approve its own proposal. If it hallucinates, the
worst case is a rejected proposal."

## 8. Digital twin — Built

**What.** A real PostgreSQL instance cloned from production, used to measure a
change before production sees it.

**How** ([pg.py](../twin/agent/pg.py), [runner.py](../twin/agent/runner.py)).
1. **Twin source.** A standby with `recovery_min_apply_delay`. WAL arrives
   immediately but is applied late, so the standby is always at a known past
   moment T0 for which the workload is already captured.
2. **Freeze.** Replay is paused, the position (LSN) and time T0 are recorded, the
   standby is stopped, its data directory is copied (`cp --reflink=auto`), and
   it is restarted.
3. **Clone.** Each arm gets a fresh copy, started with
   `recovery_target_lsn = <that position>` and `recovery_target_action = promote`.
   This matters: the copy contains WAL that had been received but not yet
   applied; without a target it would roll forward to "now".
4. **Warm.** Every relation is loaded with `pg_prewarm`, so both arms start with
   the same warm cache. Before this, whichever arm ran first paid for cold reads
   and looked about three times slower.
5. **Apply.** The same executor used for production applies the action to the
   treatment clone only.
6. **Replay** ([replay.py](../twin/agent/replay.py)). Captured transactions run
   at their original offsets from T0, as their original tenant role.
7. **Repeat** with the arm order alternating (control–treatment,
   treatment–control) so drift in the host falls on both arms alike.

**What has been observed so far (development machine, not an evaluation).**
- Replay errors are rare: a handful of duplicate-key errors per several thousand
  transactions, from concurrent writes replaying in a slightly different order.
- With no change in either arm, on a quiet machine, per-tenant p95 differed by a
  few percent. When other work ran on the machine at the same time it differed by
  up to about 25%. The noise floor is therefore something the evaluation must
  measure, not assume.

**Explain it.** "It is not a simulation. It is the same data, the same
statistics and the same queries. Cloning for verification is not my invention —
Azure SQL does it for indexes; my contribution is the per-tenant gate and
measuring what the twin is worth."

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

## 9b. Workload capture and replay — Built

**What.** PostgreSQL logs every statement with its duration and parameters as
JSON (`log_min_duration_statement=0`, `log_destination=jsonlog`).
[pglog.py](../core/dbpilot_core/pglog.py) reassembles that log into
transactions: who ran it, when, which statements, with which parameters.

**Why one capture, two uses.** The collector derives per-tenant latency
percentiles from it (which `pg_stat_statements` cannot give), and the twin
replays it.

**Details worth knowing.**
- Statements are grouped by backend process id. Behind a transaction-mode
  pooler a backend serves many clients but only one transaction at a time, so a
  `BEGIN…COMMIT` on one pid is one client transaction.
- The extended protocol logs parse, bind and execute separately; their durations
  are added together.
- The log has parameter values but not their types. Numbers are replayed as
  bare numeric literals and everything else as quoted literals. A first version
  quoted everything and a few analytical queries failed with a smallint overflow.
- OLTP and OLAP are told apart by `application_name`, which the tenant's
  connection sets.

**Limits to state openly.** Capturing every statement is expensive (about 75 MB
per minute at the development workload) and its latency cost has not been
measured yet. Concurrent writes do not replay deterministically.

## 10. Verification and statistics — Built

**What.** A decision rule over measured differences
([gate.py](../core/dbpilot_core/gate.py)).

**Key ideas.**
- *Ratio.* For each tenant and class: treatment p95 ÷ control p95. Below 1 is faster.
- *Confidence interval by block bootstrap.* Latencies close in time are
  correlated, so whole 5-second buckets are resampled, the same buckets for both
  arms, 2,000 times.
- *Superiority* for the target: the whole interval lies below 0.90.
- *Non-inferiority* for every other tenant: the whole interval lies below 1.05.
  "Not significantly worse" is not accepted.
- *Bonferroni correction.* With several tenants compared at once, each
  comparison uses a stricter level so the chance of any false "safe" stays at 5%.
- *Three outcomes.* Harm shown → reject. Benefit shown and nobody harmed →
  approve. Anything else, including too few samples → inconclusive, which is
  escalated and never applied automatically.
- *Budgets.* Storage growth and write amplification are absolute limits.
- *SLOs.* Pushing a tenant across an objective it was meeting counts as harm even
  inside the regression margin.
- *Aggregate mode* judges only the pooled workload, as single-tenant tuners do.
  It exists as the comparison point.

The tests use synthetic latency streams with a known true effect, including the
central case: target twice as fast, a neighbour 40% slower. The per-tenant gate
rejects it; the aggregate gate approves it.

**Explain it.** "A change is approved only if I can show it helps the target and
can show it does not hurt each of the others."

## 11. The engine: state machine, canary and rollback — Built

**What.** A worker ([engine.py](../controlplane/app/engine.py)) that takes
proposals through verification and, if approved, through a canary.

**State machine in the database**
([007_proposals.sql](../controlplane/migrations/007_proposals.sql)). Legal
transitions are rows in a table and a trigger enforces them. Even the database
owner cannot move a proposal from PROPOSED straight to APPLIED, revive a
rejected one, or edit an action after it was proposed. A partial unique index
allows one canary per cluster at a time, so a regression can be attributed.

**Queue.** The engine claims work with `FOR UPDATE SKIP LOCKED`, so several
engines could share the queue without taking the same proposal.

**Canary.**
1. Baseline: each tenant's p95 in the collector windows just before the change.
2. Apply with the executor; store the statements and their inverse.
3. Each window: observed p95 ÷ baseline, compared with the contract (the twin's
   prediction plus a tolerance, or a default limit if there was no twin).
4. Roll back if the contract is breached in two of three windows, or telemetry
   is missing for two windows. Otherwise mark APPLIED.

**Crash recovery.** If the engine starts and finds a proposal in CANARY, the
previous engine died while a change was live and unobserved. It rolls the change
back. Unobserved is treated as unsafe.

**Limits to state.** Canary staging is a single stage; fractional session
exposure for role settings is not implemented. The executor connects as the
database owner in the development stack.

**Explain it.** "The twin predicts; the canary checks the prediction against
reality; rollback is automatic and each action has a defined inverse."

## 12. Docker and Compose — Built

**What.** A container packages a process with its dependencies; Compose starts
several containers as one system.

**In DBPilot.** [docker-compose.yml](../docker-compose.yml) runs all three planes
on one machine: control (control-db, migrate, api, collector, engine, web), data
(dp-primary, dp-replica, pgbouncer) and experimentation (twin). `cpuset` pins
planes to different cores.

**What one machine cannot give.** The planes still share the disk and the
virtual machine. Production latency visibly rises while a twin run is in
progress. That is the reason for the three-node design, not a detail.

**Explain it.** "One command reproduces the environment, including the database
version and extensions. Isolation between planes needs separate nodes."

## 13. Kubernetes (k3s) — Written, not yet deployed

**Why here.** Three planes on three nodes. A node label places each plane; a
taint on the twin node guarantees replay load never lands next to production;
equal requests and limits give the databases Guaranteed QoS.

**Where.** [deploy/k3s/](../deploy/k3s/). The manifests parse as valid YAML and
mirror the Compose stack, but have not been applied to a cluster. One
prerequisite differs from Compose: the statement-log volume is written on the
data node and read on the other two, so it needs a ReadWriteMany storage class.

## 14. Observability — Built (database telemetry); host metrics not yet added

Database-level telemetry is collected by DBPilot itself into the control-plane
database, because it must be per-tenant and joined with proposals: query
statistics, instance counters and latency percentiles, served by the API and
drawn by the UI. Prometheus for host and container metrics is in the design and
has not been added.

## 15. The web console — Built

**What.** A React application ([web/](../web/)) served by nginx, which also
proxies `/api` so the browser talks to one origin.

**Rule it follows.** Every value on screen is read from the API. Pages show an
explicit empty state when there is no data rather than a placeholder number.
The Experiments page is computed from the outcome ledger.

**Where authorization lives.** The UI hides buttons a role cannot use, but that
is a convenience. The API checks the role, and PostgreSQL checks it again.

**Explain it.** "The interface cannot grant anything. Hiding a button is not
security; the database refusing the write is."

## 16. Cloud architecture — Built on one machine, designed for three nodes

Control plane, data plane and experimentation plane, as in
[ARCHITECTURE.md](ARCHITECTURE.md) §3. The separation is what lets the system
experiment without distorting what it measures.

---

## Try it yourself

```bash
docker compose up -d --build
docker compose run --rm dp-seed                                # load the four tenants (~5 min)
docker compose run --rm --no-deps api python -m pytest -q     # control plane, core, proposers
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
