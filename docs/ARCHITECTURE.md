# DBPilot v2 — Architecture Specification

Status: **frozen**. Changes to this document need a stated reason.
Build status of each part is tracked in the [README](../README.md).

## 1. What DBPilot is

A control plane that optimises a shared, multi-tenant PostgreSQL cluster **without
letting an optimisation for one tenant harm another**.

```
OBSERVE → DIAGNOSE → PLAN → DIGITAL TWIN → VERIFY → CANARY → MONITOR → ROLLBACK → LEARN
```

An AI agent proposes actions from a closed, typed action space. It never runs
SQL. Every proposal is measured on a real clone of the database under replayed
workload, for every tenant, before production is touched.

## 2. Research framing

**Problem.** In a shared PostgreSQL instance, tuning actions have cross-tenant
side effects, and LLM agents now propose such actions without accounting for them.

**Prior art we do not claim.** Testing a change on a clone with replayed workload
and reverting on regression is established: Azure SQL auto-indexing (SIGMOD 2019,
"B-instances"), Oracle automatic indexing (VLDB 2025, invisible indexes), DBLab
thin clones. SLO-aware multi-tenant what-if tuning exists (Tempo, VLDB 2016).

**Gap (to be confirmed by a systematic review).** Per-tenant non-inferiority
gating of LLM-agent proposals across a mixed action space, and a measured
comparison of twin vs canary vs no verification.

**Contribution.** An open PostgreSQL system implementing the loop, a scenario
suite with optimisation traps, and the empirical study below.

| # | Research question | Hypothesis (thresholds fixed after a pilot) |
|---|---|---|
| RQ1 | How often do unverified agent proposals harm some tenant? | ≥15% in trap scenarios |
| RQ2 | Does the twin predict per-tenant effects? | ≥90% sign agreement |
| RQ3 | Does a per-tenant gate stop harm an aggregate gate misses? | Yes, on S5–S8 |
| RQ4 | What does the twin add over canary alone? | Less harm exposure, slower time-to-apply |
| RQ5 | How much verification cost do cheap tiers save? | ≥50% resolved before replay |
| RQ6 | Does calibration from history improve gate accuracy? | Open |

## 3. Three planes

```
┌──────────────────────────── CONTROL PLANE ────────────────────────────┐
│  Web UI (React) ──REST/SSE──► API (FastAPI: auth, RBAC, orchestration)│
│                                   │                                   │
│   Control-plane DB (PostgreSQL): orgs, users, roles, clusters,        │
│   tenants, SLOs, telemetry, incidents, proposals, verdicts,           │
│   twin runs, canaries, outcome ledger, audit log, job queue           │
│                                   │                                   │
│  Telemetry collector ─► Agent ─► Action planner ─► Verification engine│
│  (+ shift detector)    (LLM or   (schema-validate,   T0 static rules  │
│                         rules,    compile to SQL)    T1 planner what-if│
│                         read-only                    T2 twin replay   │
│                         tools)                       T3 canary        │
│                                                  Canary controller    │
└───────────────┬───────────────────────────────────────┬───────────────┘
                │ executor (typed actions only)         │ twin jobs
┌───────────────▼──────── DATA PLANE ──────┐  ┌─────────▼─ EXPERIMENTATION PLANE ─┐
│ Workload driver ─► PgBouncer ─► primary  │  │ Twin node agent                   │
│                         │         │      │  │   delayed standby (twin source)   │
│                         └► replica◄┘     │  │   ├─ clone → CONTROL instance     │
│ statement log (capture) ─────────────────┼──┼─► └─ clone → TREATMENT instance   │
│ primary ──streaming replication──────────┼──┼─►  replayer + per-tenant metrics  │
└──────────────────────────────────────────┘  └───────────────────────────────────┘
Observability: Prometheus + postgres/cgroup exporters across all planes.
```

**Why three planes.** Twin replay is real load. If it shares CPU or disk with
production it distorts the very measurements the canary relies on. Development
runs all planes on one host under Docker Compose with disjoint CPU sets;
evaluation and cloud deployment use three k3s nodes with the twin node tainted so
that only twin workloads can be scheduled there.

## 4. Data plane

- PostgreSQL 18, one shared database, CH-benCHmark-derived schema.
- Large tables are range-partitioned by warehouse id; a tenant owns a contiguous
  warehouse range, hence its own partitions.
- One PostgreSQL role per tenant. `pg_stat_statements` keys on role, so every
  statistic is per-tenant without extra instrumentation. RLS confines a role to
  its warehouses.
- PgBouncer in front: per-tenant connection caps, session-fraction canaries and
  replica routing are all applied here.
- One streaming replica for read routing; one delayed standby as twin source.

## 5. Observations (agent tools are read-only)

| Signal | Source | Cadence |
|---|---|---|
| Per-tenant query fingerprints (calls, time, rows, buffers, temp, WAL) | `pg_stat_statements` deltas | 60 s |
| Per-tenant latency percentiles | sampled statement log | 60 s |
| Plans of slow queries | `auto_explain` JSON; `EXPLAIN` on demand | event |
| Waits and blocking chains | `pg_stat_activity`, `pg_locks` | 1 s |
| Table/index profile, unused indexes | `pg_stat_user_*`, catalog | 5 min |
| Instance health, WAL, checkpoints, lag, settings | `pg_stat_wal/io/replication`, `pg_settings` | 60 s |
| CPU, memory, I/O per container | cgroup metrics | 15 s |
| SLOs and burn rate | control-plane DB | 60 s |
| Workload shift | divergence of fingerprint mix between windows | 5 min |
| Past proposals and outcomes | outcome ledger | on demand |

The agent sees literal-stripped fingerprints, never row data or parameter values.

## 6. Action space

| ID | Action | Scope | Inverse |
|---|---|---|---|
| A0 | No action / escalate | — | — |
| A1 | Create B-tree index | one tenant partition, or all | drop |
| A2 | Drop unused index (never constraint-backing) | same | recreate from stored definition |
| A3 | Role-level setting, allowlisted and bounded | one tenant | previous value |
| A4 | Instance setting, reload-only, allowlisted and bounded | instance | previous value |
| A5 | `ANALYZE`, statistics target, per-table autovacuum | one table | previous value |
| A6 | Route a tenant's read-only class to the replica | one tenant | route back |
| A7 | Per-tenant concurrency cap at the pooler | one tenant | previous value |
| A8 | Query rewrite | advisory only | — |

Each action is a JSON object validated against a schema, compiled to SQL by a
deterministic executor, limited by bounds, paired with an inverse, and audited.
Excluded: arbitrary SQL, table DDL, restart-requiring settings, materialised
views, tenant migration.

## 7. Digital twin

1. **Twin source.** A standby with `recovery_min_apply_delay` (≈15 min). It is
   always at "production, 15 minutes ago", and the statement log already holds
   the workload for those 15 minutes.
2. **Clone.** The node agent stops the twin source at a recorded replay point,
   copies its data directory twice (`cp --reflink=auto`: instant on XFS/btrfs,
   plain copy elsewhere), and restarts it.
3. **Control and treatment.** Both copies are promoted to standalone instances.
   The executor applies the proposed action to the treatment copy only.
4. **Replay.** The captured window is replayed against each with original timing,
   sessions and per-tenant roles, interleaved A-B-A-B, after a warm-up segment.
5. **Measure.** Per tenant and query class: latency percentiles, throughput,
   errors, WAL bytes per transaction, temp usage, index size, build time, CPU, I/O.
6. **Fidelity.** A/A runs give the noise floor. Known good, neutral and harmful
   actions are run on the twin and on production; we report sign agreement, rank
   correlation and error per tenant and action type.

Known limits: replay of concurrent writes is not deterministic; a verification
takes minutes, so DBPilot is for optimisation, not second-scale incident response.

## 8. Verification engine

Tiers run cheapest first; each returns APPROVE, REJECT or INCONCLUSIVE.

| Tier | Check |
|---|---|
| T0 | Schema-valid, within bounds, worst-case memory arithmetic, storage budget, no duplicate index, one action in flight |
| T1 | HypoPG and `EXPLAIN` for affected fingerprints of all tenants |
| T2 | Twin replay |
| T3 | Canary |

T2 approves only if all hold:
- **Target benefit:** lower confidence bound of improvement exceeds a minimum effect.
- **Non-inferiority for every other tenant:** upper confidence bound of any
  regression is under the margin and each SLO is predicted to hold (corrected for
  multiple comparisons).
- **Budgets:** storage, WAL per transaction, build cost, replica lag, peak memory.
- **Uncertainty:** an interval straddling a threshold extends the replay up to a
  budget, then escalates. Uncertain is never treated as safe.
- **Reversibility:** actions with expensive inverses face tighter margins or
  mandatory human approval.

The engine emits a **rollback contract**: per-tenant thresholds derived from the
twin's prediction, which the canary enforces.

## 9. Canary and rollback

| Action | Stage 1 | Stage 2 |
|---|---|---|
| A1 | target tenant's partition | other partitions |
| A3 | fraction of the tenant's sessions | all sessions |
| A4 read path | replica, with routed reads | primary |
| A4 write path | time-boxed on primary | confirm |
| A6, A7 | percentage of traffic | full |

Automatic rollback when: a tenant breaches its contract in 2 of 3 windows;
errors, deadlocks or lock waits spike; replica lag exceeds budget; the observed
effect deviates from the twin's prediction beyond tolerance; telemetry is lost;
or the canary is not confirmed by its deadline.

## 10. Learning

Every proposal stores: observations → proposal → tier verdicts → twin prediction
→ canary result → production result.

- **Now:** twin calibration (empirical twin-vs-production error per action type,
  used to size the gate's intervals) and agent memory (SQL lookup of similar past
  outcomes).
- **Later, if history is large enough:** a classifier that predicts rejection so
  hopeless proposals skip replay.

## 11. Workload and scenarios

CH-benCHmark-derived: TPC-C transactions plus a subset of the CH analytical
queries over the same tables, driven by an open-loop, tenant-aware generator.
Results are "CH-benCHmark-derived", never TPC results.

Tenants: steady OLTP, bursty OLTP, analytical, mixed and growing.

| # | Scenario | Correct outcome |
|---|---|---|
| S1 | Missing index | apply |
| S2 | Stale statistics after growth | analyze |
| S3 | Noisy-neighbour burst | concurrency cap |
| S4 | Workload shift | re-plan |
| S5 | Sort/hash spills | role-level approved, instance-level rejected |
| S6 | Trap: index helps OLAP, taxes OLTP writers | reject or scope |
| S7 | Trap: parallelism helps OLAP, starves OLTP | reject or scope |
| S8 | Trap: replica routing under lag | reject or bound |
| S9 | Transient spike | no action |

## 12. Evaluation

| Config | Proposer | Verification |
|---|---|---|
| C0 | none (defaults) | — |
| C1 | rule-based | none |
| C2 | agent | none |
| C3 | agent | canary only |
| C4 | agent | twin + per-tenant gate + canary |
| C4-agg | agent | twin + aggregate gate + canary |
| C1+V | rule-based | twin + per-tenant gate + canary |

Metrics: SLO violation rate, harmful-action rate, benefit retained, tenant harm
exposure, twin accuracy and sign agreement, time-to-apply, verification cost,
rollback rate, LLM cost, resource impact. Runs ≥10 minutes, ≥10 repetitions,
confidence intervals, paired tests. No number is reported that the harness did
not produce.

## 13. Technology

| Technology | Why | Status |
|---|---|---|
| PostgreSQL 18 (+ `pg_stat_statements`, `auto_explain`, HypoPG) | target DBMS and control-plane store | required |
| PgBouncer | per-tenant caps, canary fractions, routing | required |
| FastAPI, psycopg 3 | control-plane API | required |
| React, Vite, TypeScript | control-plane UI | required |
| LLM tool calling behind a proposer interface | the agent; rule-based proposer shares the interface | required |
| scipy / statsmodels | intervals, non-inferiority tests | required |
| Prometheus + exporters | host and container metrics | required |
| Docker Compose | development | required |
| k3s | three-node plane isolation | required for evaluation |
| Gradient boosting | rejection pre-screen | later |
| Vector DB, agent frameworks, Kafka, CloudNativePG, BenchBase | no purpose in this design | not used |

## 14. Implementation notes: where the build differs from this specification

Recorded so that the specification and the code can be compared honestly.

| Specification | As built | Why |
|---|---|---|
| A7 concurrency cap applied at the pooler | `ALTER ROLE … CONNECTION LIMIT`, then the pooler recycles its connections | One SQL statement with an exact inverse, and it can be reproduced on the twin, where there is no pooler |
| A6 replica routing verified on the twin | Defined as an action, treated as advisory | A single twin instance has no replica to route to |
| A8 query rewrite with twin evidence | Defined as an action, advisory, no twin evidence | Not built |
| Canary staged per action (partition first, session fraction, replica first) | An index for every tenant is staged: one partition (the tenant the twin predicts to gain most), then the rest, each stage with its own canary windows. Every other action is one stage: applied, observed, kept or undone | A fraction of one tenant's sessions cannot be selected at the pooler, and there is no routed read traffic to canary on the replica |
| Six rollback triggers | Contract breach in 2 of 3 windows (the contract is the twin's prediction plus a tolerance, so this is also the deviation-from-prediction trigger); deadlocks per window; replica lag; lost telemetry; engine restart during canary | Statement error rates are not collected, so there is no error-spike trigger |
| Twin delay about 15 minutes | Configurable; the development stack uses 60 seconds | Faster iteration; the Kubernetes manifest sets 900 |
| Inconclusive verdict extends the replay up to a budget | Up to three replays of fresh windows are judged together; each look is tested at a third of the error rate, so looking repeatedly does not inflate false approvals | As specified |
| Executor with its own least-privilege role | `dbpilot_executor`: not a superuser; `ALTER SYSTEM` only on the allowlisted parameters; ADMIN on tenant roles only; DDL limited to `CREATE INDEX` and `DROP INDEX` by an event trigger | PostgreSQL has no privilege narrower than table ownership for `CREATE INDEX`, so as a member of the owning role it could still read or change rows |
| Observations include waits, locks, sampled plans, workload-shift detection | Query statistics, instance counters, latency percentiles, on-demand plans, table profiles, settings | The rest is not built |
| Prometheus for host metrics | Not added | Database telemetry is collected by DBPilot itself |
| Calibration of the gate from the outcome ledger | The canary tolerance for an action type is the 90th percentile of the twin's past relative error for that type, once the ledger holds 8 twin-versus-production pairs; below that, the default 10% | The gate's own margins (10% benefit, 5% regression) are fixed policy, not calibrated |
| Three nodes | One machine under Compose; Kubernetes manifests written, not deployed | No cluster was available |
| Evaluation: ≥10 repetitions, ≥10-minute runs | Harness built; only single pilot trials have been run | Running the full matrix takes tens of hours on isolated hardware |
