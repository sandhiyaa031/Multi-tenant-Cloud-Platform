# Key concepts

Each entry says what the term means, then where it appears in DBPilot. File paths are relative to the repository root.

## Databases

**DBMS (database management system).** The software that stores data and answers queries: here, PostgreSQL 18. It
decides *how* to run each query (the *plan*), keeps frequently used data in memory, and writes every change to a log
before applying it.

**Query plan and planner.** For one SQL query there are many ways to get the answer (read the whole table, or use an
index). The planner estimates the cost of each and picks one. `EXPLAIN` shows the choice. DBPilot uses the planner's
estimate as a cheap first check (T1) in `twin/agent/whatif.py`.

**Index.** A sorted lookup structure on one or more columns. It makes reads that filter on those columns fast and makes
every write to the table slightly slower, because the index must be updated too. The demo's change is an index on
`order_line(ol_i_id)` for one tenant.

**Hypothetical index (HypoPG).** A PostgreSQL extension that lets the planner pretend an index exists, so you can ask
"would you use it?" without building it.

**Write-ahead log (WAL) and replication.** Every change is first written to a log. A *replica* (or *standby*) is a
second server that receives the log and replays it, so it holds the same data a moment later. DBPilot has a normal
replica (`dp-replica`) and a deliberately delayed standby used as the source of twin clones.

**Connection pooler (PgBouncer).** Opening a database connection is expensive, so applications connect to a pooler that
shares a small number of real connections. In DBPilot tenants can only connect through it.

**Latency percentiles (p50, p95, p99).** Sort all request times; p95 is the value 95% of requests were faster than. It
describes the slow end, which is what users notice. Averages hide it. DBPilot's objectives and the gate use p95.

**SLO (service level objective).** A promise such as "99% of this tenant's transactions finish under 100 ms". Stored in
the `cp.slos` table.

**OLTP and OLAP.** OLTP is many small transactions (placing an order). OLAP is few large analytical queries (a sales
report). They stress a database differently, so DBPilot measures them as separate *classes* per tenant, written
`t_mixed/OLAP`.

**TPC-C and CH-benCHmark.** TPC-C is a standard OLTP workload modelled on a wholesale supplier (warehouses, districts,
orders). CH-benCHmark adds analytical queries on the same tables. DBPilot's schema and workload are derived from them
(`dataplane/postgres/initdb/02_schema.sql`, `workload/workload/tpcc.py`, `olap.py`). It is not an audited benchmark.

## Multi-tenancy

**Tenant.** One customer of a shared service. The demo has four: `t_steady`, `t_bursty`, `t_analytic`, `t_mixed`.

**Multi-tenancy.** Several tenants in one database server. Cheaper than a server each; the cost is that they affect one
another (the *noisy neighbour* problem).

**Role per tenant.** A PostgreSQL role is a login identity. Each tenant has its own, so PostgreSQL's own statistics are
already split by tenant and a setting can be changed for one tenant only.

**Row-level security (RLS).** A rule attached to a table that filters which rows a role may see or change. Each tenant
owns a range of warehouse ids, and the policy limits its role to that range. Used twice in DBPilot: between tenants in
the data plane (`dataplane/postgres/initdb/02_schema.sql`), and between organizations in the control plane
(`controlplane/migrations/003_security.sql`).

**Partitioning.** One logical table stored as several physical pieces. The big tables have one partition per tenant, so
an index can be built for one tenant alone.

**Organization, cluster, tenant.** In the control plane: an *organization* is a DBPilot customer (a team that signs in),
a *cluster* is one PostgreSQL deployment it manages, and *tenants* are that cluster's customers.

## Proposing changes

**Action space.** The closed list of kinds of change a proposer may suggest, defined as typed classes in
`core/dbpilot_core/actions.py`: create index, drop index, role setting, instance setting, analyze, concurrency cap,
replica routing, query rewrite, no action. Anything else fails validation and never reaches a database.

**Executor and inverse.** The function `plan()` in `actions.py` turns an action into the SQL that applies it and the SQL
that undoes it (the *inverse*), reading the current state first so undoing restores exactly what was there.

**Rule-based proposer.** Fixed rules over the telemetry (`controlplane/app/proposers/rules.py`): for example, an
expensive read that filters an unindexed column and that the planner confirms an index would help.

**LLM proposer (agent).** A language model given ten read-only tools (latency, top queries, plans, what-if, history...)
and one tool with an effect, `propose_action` (`controlplane/app/proposers/agent.py`). Its output goes through exactly
the same validation and verification as a rule's. It is tested against a scripted stand-in model. **It has never been
run against a live model in this project**, because no API key was available.

## Verifying changes

**Verification.** Checking a proposal with evidence before it reaches production. Four tiers, cheapest first (T0 to T3),
run by `Engine.verify` and `Engine.canary` in `controlplane/app/engine.py`.

**Digital twin.** Here: a real PostgreSQL copy of production, not a simulation. A delayed standby is frozen at a known
moment and copied; the copy is started as an independent database (`twin/agent/pg.py`).

**Replay.** Running the transactions captured from production's statement log against a clone, as the same tenant roles
and at the same relative times (`twin/agent/replay.py`).

**Control and treatment arms.** The two clones in a comparison. Control is unchanged; treatment has the proposed change.
Everything else is the same, so a difference is attributed to the change.

**Replay pair.** One control replay and one treatment replay of the same captured window. The gate's unit of evidence.

**A/A test.** A comparison where both arms are identical. Any difference reported is produced by the measurement itself.
It is how the first gate was found to be wrong (`docs/evidence/e1/`).

**Confidence interval.** A measurement of a noisy quantity gives a range, not a point. "Ratio 0.95, interval 0.76 to
1.19" means the data are consistent with the tenant being 24% faster or 19% slower. The more the repeated measurements
agree, and the more of them there are, the narrower the range.

**Student's t interval.** The standard way to build that range from a small number of repeated measurements. The gate
applies it to the logarithm of each pair's ratio (`compare` in `core/dbpilot_core/gate.py`).

**Non-inferiority.** Showing that something is *not worse by more than a margin*. It requires the whole interval to lie
below the margin. This is much harder than detecting a large improvement, and it is the reason the gate so often says
"uncertain". The gate's margins: target must be shown at least 10% faster; every other tenant's interval must lie
entirely below 1.05.

**Bonferroni correction.** If you test many things, the chance that one is wrong by luck rises. Dividing the allowed
error among the tests keeps the overall chance at 5%. The gate does this across tenants and across looks.

**Verdicts.** APPROVE (benefit shown, nobody harmed), REJECT (harm shown, or no benefit possible), INCONCLUSIVE (could
not show either; not applied, a person decides).

**Per-tenant gate and aggregate gate.** The first judges every tenant and class separately. The second judges the pooled
workload. The comparison between them is the research question.

**Semi-synthetic analysis.** Real measurements with an artificial effect added by arithmetic. Used in the closing
evaluation (`tools/final/power.py`) because no real harmful change was found. It tells you how the decision rule
behaves; it does not tell you the twin reproduces production.

## Applying changes

**Canary.** Applying a change to production under watch. The engine records each tenant's p95 before the change (the
*baseline*), applies it, and compares each following collector window with a limit (the *contract*).

**Contract.** The largest ratio to baseline each tenant is allowed. It comes from the twin's verdict plus a tolerance,
or a default (1.15) when there was no twin.

**Rollback.** Running the stored inverse. Automatic when the contract is broken in two of three windows or telemetry is
lost; on request from an operator later; and automatic at engine start-up for any change left mid-canary.

**Outcome ledger.** A database view (`cp.outcome_ledger` in `controlplane/migrations/007_proposals.sql`) joining each
proposal with its verification steps, twin prediction and production result. The Experiments page reads it.

## Evidence

**Unit and integration tests.** Code that checks the software does what it was written to do: a viewer cannot write, a
clone starts at the right moment, the gate rejects a synthetic harmful input. They answer "is it built correctly?".
DBPilot has 286 of them plus browser tests.

**Experimental evidence.** Measurements of how the system behaves on real workload: how often the gate is right, how
much noise there is. They answer "does the idea work, and how well?". Passing tests say nothing about this. DBPilot's
tests all pass, and its experiments still showed the first gate giving false alarms.

**Pre-registration (frozen protocol).** Writing down what will be measured, how it will be judged and what is expected
*before* running it, so the result cannot shape the analysis afterwards. Each run in `docs/evidence/` has one.

**Negative result.** A finding that something did not work or could not be shown. Reported as such in this project; see
[REPORT.md](REPORT.md) section 5.
