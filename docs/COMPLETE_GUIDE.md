# DBPilot from A to Z

One document that takes you from "what is this project" to being able to explain any part of it, run it, and defend
its results. It assumes you know what a table and a SQL query are. Everything else is explained when it first appears.

It is long on purpose. Read Parts 1 to 4 in order; after that each part stands by itself.

| Part | What you will be able to do after it |
|---|---|
| [1. The problem](#part-1-the-problem) | Say in one minute why the project exists |
| [2. Background you need](#part-2-background-you-need) | Understand every term used later |
| [3. The design in one picture](#part-3-the-design-in-one-picture) | Draw the system on a whiteboard |
| [4. The life of one change](#part-4-the-life-of-one-change) | Follow a proposal through every file it touches |
| [5. Each component in depth](#part-5-each-component-in-depth) | Open any folder and know what is in it |
| [6. The gate, with the arithmetic](#part-6-the-gate-with-the-arithmetic) | Compute a verdict by hand |
| [7. The story of the experiments](#part-7-the-story-of-the-experiments) | Say what was measured, in order, and what each run taught |
| [8. What is shown and what is not](#part-8-what-is-shown-and-what-is-not) | State the results without overclaiming |
| [9. The LLM agent](#part-9-the-llm-agent) | Explain how a language model is contained |
| [10. Security](#part-10-security) | Explain every isolation boundary |
| [11. Measuring properly: benchmark mode](#part-11-measuring-properly-benchmark-mode) | Explain the current work and its protocol |
| [12. Running it](#part-12-running-it) | Start, demo, test and stop the system |
| [13. Where everything is](#part-13-where-everything-is) | Find any file |
| [14. Mistakes worth remembering](#part-14-mistakes-worth-remembering) | Tell the honest story of what went wrong |
| [15. What comes next](#part-15-what-comes-next) | Describe the remaining plan |
| [16. A study plan](#part-16-a-study-plan) | Learn the codebase in a sensible order |
| [Glossary](#glossary) | Look up a word |

Numbers in this guide come from `docs/evidence/`. If this guide and the evidence ever disagree, the evidence is right.

---

## Part 1. The problem

**One PostgreSQL server, many customers.** A company that sells software to many customers usually stores all of them
in one database server, because a server per customer costs too much. Each customer is called a *tenant*. Tenants
share the server's memory, its processor cores, its disk and its settings.

**Tuning helps one and can hurt another.** Tuning means changing the database so queries run faster: adding an index,
giving sorts more memory, allowing more parallel workers. In a shared server such a change is rarely free for
everyone:

| A change that helps one tenant | How it can hurt the others |
|---|---|
| An index that speeds up a reporting tenant's reads | Every write to that table must now also update the index |
| More memory per sort for heavy reports | Less memory left when many small transactions run at once |
| More parallel workers per query | Fewer cores for latency-sensitive transactions |
| Sending reports to a read replica | Stale results, or queries cancelled when the replica lags |

**An average hides the victim.** If you judge a change by the server's overall speed, a large gain for one tenant
outweighs a moderate loss for another, and the change looks like a win.

**The tuner is now often a machine.** Cloud providers apply indexes automatically, and language-model systems read
statistics and propose settings. They are evaluated on one workload with one overall number. A language model's
explanation of why its change is safe is text, not a measurement.

**The research question.** Does judging a change *tenant by tenant*, on a copy of the database, before it reaches
production, add anything over judging the workload as a whole, or over simply trying it in production carefully?

---

## Part 2. Background you need

### Databases

- **PostgreSQL.** The database server used throughout (version 18).
- **Query plan.** For one query there are many ways to get the answer. The *planner* estimates the cost of each and
  picks one. `EXPLAIN` shows it.
- **Index.** A sorted lookup structure on some columns. Reads that filter on those columns get faster; every write to
  the table gets a little slower.
- **Hypothetical index.** With the HypoPG extension the planner can pretend an index exists, so you can ask "would you
  use it?" without building it.
- **Write-ahead log (WAL).** Every change is written to a log before it is applied. A *replica* or *standby* is a
  second server that receives that log and replays it, so it holds the same data a moment later.
- **Shared buffers.** PostgreSQL's own cache of table and index pages. When data fits in it, reads are fast.
- **Connection pooler (PgBouncer).** Opening a database connection is expensive, so applications connect to a pooler
  that shares a few real connections among many clients.
- **Latency percentiles.** Sort all request times. p50 is the middle one, p95 the value 95% of requests were faster
  than. The tail (p95, p99) is what users notice; averages hide it.
- **SLO.** A promise such as "99% of this tenant's transactions finish in under 100 ms".
- **OLTP and OLAP.** Many small transactions (placing an order) versus few large analytical queries (a sales report).
  DBPilot measures them as separate *classes* per tenant, written `t_mixed/OLAP`.
- **TPC-C and CH-benCHmark.** A standard order-processing workload (warehouses, districts, orders, stock) and its
  extension with analytical queries. DBPilot's schema and workload are derived from them. It is not an audited
  benchmark.
- **Round trip.** One request from the application to the database and its answer. A transaction with 36 statements
  makes about 36 of them.

### Multi-tenancy

- **Role.** A PostgreSQL login identity. Each tenant has its own.
- **Row-level security (RLS).** A rule on a table that filters which rows a role may see or change. Here each tenant
  owns a range of warehouse ids and its role is limited to that range.
- **Partitioning.** One logical table stored as several physical pieces. The six large tables have one partition per
  tenant, so an index can be built for one tenant alone.

### Statistics

- **Control and treatment.** Two copies that differ only in the change being tested. A difference between them is
  attributed to the change.
- **A/A test.** Both copies are identical. Whatever difference the system reports is its own invention. It measures
  how much noise the method has.
- **Confidence interval.** A measured quantity with noise is a range, not a point. "Ratio 0.95, interval 0.76 to 1.19"
  means the data are consistent with 24% faster and with 19% slower.
- **Non-inferiority.** Showing something is *not worse by more than a margin*. The whole interval must lie below the
  margin. Much harder than spotting a big improvement.
- **Pre-registration.** Writing down what will be measured and how it will be judged *before* running it, so the result
  cannot shape the analysis.

---

## Part 3. The design in one picture

```
            proposer (rules or LLM)                         chooses ONE of nine typed actions; never writes SQL
                     |
                     v
   T0  static rules ............ valid? known tenant? within limits?          milliseconds
   T1  planner what-if ......... would the planner even use this index?       seconds
   T2  digital twin ............ replay real traffic on two copies,           minutes
                                 measure every tenant, the GATE decides
   T3  canary .................. apply in production, watch every tenant,     minutes
                                 undo automatically if a limit is broken
                     |
                     v
              outcome ledger ...... what the twin predicted vs. what production did
```

Four rules hold everywhere:

1. **The proposer never executes SQL.** It emits a typed action; a deterministic *executor* turns it into SQL.
2. **The proposer cannot approve its own proposal.** Verification is separate code.
3. **Every tenant is measured**, not only the one being helped.
4. **Uncertain is not safe.** A result that cannot be told apart from harm is not approved.

The system is split into three *planes*, each a group of containers:

| Plane | Containers | Job |
|---|---|---|
| Control | `web`, `api`, `control-db`, `collector`, `engine` | Decides. Holds users, tenants, telemetry, proposals, audit |
| Data | `dp-primary`, `dp-replica`, `pgbouncer`, load generators | Serves tenants. Is the thing being protected |
| Experimentation | `twin` | Tests changes on copies, so testing never disturbs what it measures |

---

## Part 4. The life of one change

Follow one proposal: *create an index on `order_line(ol_i_id)` for tenant `t_analytic`*.

**Step 0. Telemetry exists.** The primary logs every statement as JSON with its duration. The **collector**
(`controlplane/app/collector.py`) reads `pg_stat_statements` and that log every 20 seconds and writes per-tenant
query statistics and latency percentiles into the control database.

**Step 1. A proposal is created.** Three ways, all in `controlplane/app/routers/proposals.py`:
- by hand in the console (`POST /clusters/{id}/proposals`);
- by the rule-based proposer (`POST /clusters/{id}/diagnose`, `source: "rule"` → `proposers/rules.py`);
- by the LLM agent (same endpoint, `source: "agent"` → `proposers/agent.py`).

`insert_proposal` validates the action with `actions.parse_action` and inserts a row in state `PROPOSED`. The API
never verifies or applies anything.

**Step 2. The engine claims it.** `controlplane/app/engine.py: Engine.work_once` moves it to `VERIFYING` with
`FOR UPDATE SKIP LOCKED`, so two engines could never take the same proposal.

**Step 3. T0, static rules** (`t0_static`). Is the action valid, the tenant known, the action not already applied, and,
for memory settings, would the worst case exhaust RAM?

**Step 4. T1, planner what-if.** For an index only. The engine sends the observed queries on that table to the twin
agent (`twin/agent/whatif.py`), which plans each with and without a hypothetical index. If no query would use it,
the proposal is `REJECTED`.

**Step 5. T2, the digital twin.** `Engine.twin_run` asks the twin agent for a *replay pair*:
1. `pg.freeze_base()` pauses the delayed standby at a known moment T0 and copies it.
2. `pglog.read_window` reads the transactions production really ran after T0.
3. A clone is started from the copy; for the treatment arm the action is applied with the same executor used later
   in production; both clones are warmed.
4. `replay.py` runs every transaction as its tenant at its original offset and times it.

The engine adds pairs one at a time (up to `TWIN_MAX_LOOKS`, 8 on the laptop) and after each asks the gate.

**Step 6. The gate** (`core/dbpilot_core/gate.py`) returns APPROVE, REJECT or INCONCLUSIVE (Part 6).

**Step 7. A person decides, when needed.** An operator can approve a proposal that passed. Only an admin can override
an INCONCLUSIVE one, and the reason typed is written to the audit log.

**Step 8. T3, the canary** (`Engine.canary`). The engine records each tenant's p95 just before the change (the
*baseline*), applies the action in production as the least-privilege role `dbpilot_executor`, stores the SQL that
undoes it, and compares each following collector window with a limit (the *contract*). A breach in two of three
windows, or lost telemetry, runs the inverse.

**Step 9. The ledger.** A database view joins the proposal, the twin's prediction and production's result. The
Experiments page reads it.

The states a proposal can be in, and the only moves allowed between them, are enforced by a database trigger
(`controlplane/migrations/007_proposals.sql`), not by application code:

```
PROPOSED → VERIFYING → REJECTED | INCONCLUSIVE | AWAITING_APPROVAL | APPROVED | ADVISORY
AWAITING_APPROVAL → APPROVED | REJECTED          INCONCLUSIVE → APPROVED (admin) | REJECTED
APPROVED → CANARY → APPLIED | ROLLED_BACK        APPLIED → ROLLBACK_REQUESTED → ROLLED_BACK
```

---

## Part 5. Each component in depth

### 5.1 The action space (`core/dbpilot_core/actions.py`)

A proposer may only express one of these. Anything else fails validation and never reaches a database.

| Action | Scope | How it is undone |
|---|---|---|
| `no_action` | — | — |
| `create_index` | one tenant's partition, or all | drop it |
| `drop_index` (never one enforcing a constraint) | one index | recreate from its stored definition |
| `role_setting` (work_mem, parallel workers per gather, statement_timeout, random_page_cost, jit; each bounded) | one tenant | previous value |
| `instance_setting` (a short allowlist of reload-only settings, each bounded) | whole instance | previous value |
| `analyze` | one table or partition | nothing to undo |
| `concurrency_cap` | one tenant | previous value |
| `replica_routing` | one tenant | advisory only |
| `query_rewrite` | — | advisory only |

`plan(action, conn)` reads the current state and returns the SQL to apply **and** the SQL to undo (the *inverse*).
`run(statements, conn)` executes them. The same two functions are used on the twin's clone and on production, so what
was verified is what gets applied.

### 5.2 The data plane (`dataplane/`)

- `postgres/initdb/02_schema.sql`: the `ch` schema, one partition per tenant for the six big tables, row-level
  security, and the table `ch.tenant_map` that says which role owns which warehouses.
- `postgres/executor.sql`: the role the engine uses in production. It is not a superuser; an event trigger refuses
  every DDL from it except `CREATE INDEX` and `DROP INDEX`.
- `pgbouncer/`: the only door tenants use; it admits tenant roles only.
- Four demo tenants: `t_steady` (steady OLTP), `t_bursty` (OLTP with bursts), `t_analytic` (reports), `t_mixed` (both).

### 5.3 The workload (`workload/`)

`driver.py` generates traffic as each tenant through the pooler. It is **open-loop**: arrivals follow a schedule that
does not depend on how fast the database answers, and latency is measured from the scheduled time. (A closed-loop
generator slows down when the database is slow and under-reports exactly the latencies that matter.)
`tpcc.py` holds the five TPC-C transactions, `olap.py` the analytical queries, `profiles/` the rates per tenant.
`evaluation/` is the trial harness: run a scenario's workload, submit its action through the public API, follow it to
its final state, undo it, and record client-side latency before, during and after.

### 5.4 Telemetry (`controlplane/app/collector.py`, `core/dbpilot_core/pglog.py`)

- `pg_stat_statements` counters only grow, so the collector stores the *difference* between snapshots.
- It has no percentiles. Percentiles come from the statement log: `pglog.Assembler` groups logged statements into
  transactions per tenant and class.
- The same log is what the twin replays.

### 5.5 The digital twin (`twin/agent/`)

A real PostgreSQL instance, not a simulation.

- `pg.py`: a *source* standby that deliberately applies production's changes late, so it always sits at a past moment
  for which the workload is already on file; `freeze_base` copies it; `fresh_work` starts a clone stopped exactly at
  the frozen position.
- `replay.py`: replays transactions and times each one, and each statement per query template.
- `runner.py`: one run. Three ways to run the two arms (Part 11).
- `whatif.py`: the planner what-if and query plans.
- `app.py`: the small HTTP API the engine calls.

### 5.6 The control plane (`controlplane/`)

- `app/main.py`, `routers/`: the REST API. Every request is authorised twice: the API checks the member's role, and
  PostgreSQL's row-level security filters rows to the caller's organization.
- `migrations/`: the control database's schema. Audit rows are written by triggers and cannot be changed, even by the
  database owner.
- `app/engine.py`: verification, canary, rollback, recovery after a crash.
- `app/observe.py`: the read-only view of a cluster that both the API and the proposers use.
- `app/planning.py`: two reading aids that decide nothing: how many replay pairs a verdict would have needed, and how
  often the canary's rule fires on telemetry in which nothing changed.

### 5.7 The console (`web/`)

React. `pages/Observe.tsx` (overview, tenants, workloads, queries, health), `pages/Optimise.tsx` (agent console,
recommendations, the proposal page, twin lab, verification, canaries, experiments), `pages/Admin.tsx` (audit,
settings). The proposal page is the centre of a demo: lifecycle, verification steps, the twin's per-tenant intervals,
the canary's windows.

---

## Part 6. The gate, with the arithmetic

File: `core/dbpilot_core/gate.py`. The unit of evidence is the **replay pair**: one control replay and one treatment
replay of the same captured window.

**For one tenant and class:**

1. In every pair, compute `ratio = treatment p95 / control p95`. Below 1 means faster.
2. Take the natural logarithm of each ratio. (Ratios multiply; their logarithms add, which is what an average needs.)
3. With n pairs, mean m and standard deviation s of those logarithms, the interval is
   `m ± t × s / √n`, where t is Student's t for n − 1 degrees of freedom.
4. Convert back with `exp`. With one pair there is no s, so there is no interval: **one pair decides nothing**.

**Worked example.** Four pairs gave ratios 0.95, 1.06, 0.99, 1.04. Logs: −0.051, 0.058, −0.010, 0.039. Mean 0.009,
standard deviation 0.049. For a plain 95% interval t(3) = 3.18, so the half-width is 3.18 × 0.049 / 2 = 0.078. The
interval on the ratio is exp(0.009 ± 0.078) = 0.93 to 1.09. It crosses 1.05, so this tenant is **not shown unharmed**,
even though its ratio is 1.01.

**The corrections.** Testing several tenants and looking several times each raise the chance of a lucky "safe". The 5%
error allowance is divided among the tenant comparisons and among the looks allowed (Bonferroni), which widens every
interval.

**The verdict:**

| Finding for a tenant | Condition |
|---|---|
| BENEFITS (target only) | whole interval below 0.90 |
| SAFE | whole interval at or below 1.05 |
| HARMED | whole interval above 1.05, **and** a pair above 1.05 exists with each arm order; or an SLO control met is broken in both orders |
| UNCERTAIN | anything else |

- any HARMED → **REJECT**
- else any UNCERTAIN → **INCONCLUSIVE** (not applied; a person decides)
- else a benefit shown → **APPROVE**
- else no benefit possible → **REJECT**

Storage growth and write volume are absolute budgets checked first. An **aggregate** mode judges only the pooled
workload, as single-tenant tuners do; it exists as the comparison.

**Why this gate and not the first one.** The first gate built its interval from the scatter of requests *inside* one
replay. But two replays run one after the other differ by themselves, by 5 to 12% on the laptop. The first gate could
not see that and reported harm between identical copies. Counting requests instead of pairs is like measuring one
patient a thousand times and claiming a thousand patients.

---

## Part 7. The story of the experiments

In the order they happened. Each has a folder under `docs/evidence/` with its protocol.

| # | What was run | What it taught |
|---|---|---|
| 1 | Pilot trials of "trap" scenarios (`p01`) | No trap harmed a neighbour. The parallelism changes did nothing: the row-security helper functions are marked parallel-unsafe, so tenant queries never run in parallel. An early "1.3× slowdown" was machine drift |
| 2 | Raising the load (`p01d`) | A lock convoy on each tenant's few warehouses collapses the run instead of degrading it |
| 3 | Characterising load levels (`p01e`, `p01f`, `p01g`) | The same workload used 220% to 396% of a core in different sessions |
| 4 | A CPU soak with the database idle (`p01h`) | The laptop's speed varies 5-24% with CPU frequency. It is not a controlled machine |
| 5 | **E1: 30 A/A twin runs** (`e1`) | The first gate declared harm in 2 of 9 verdicts between identical arms |
| 6 | Re-judging E1 with the pair gate (`e1/rejudge.txt`) | 0 false calls in 3,400 reordered sequences, and no decisive verdict either |
| 7 | Closing evaluation, Part A (`final/power.*`) | With a slowdown injected into real A/A pairs: the aggregate gate approved a 20% slowdown of a neighbour in 20-43% of sequences; the per-tenant gate approved none, and nothing harmless either |
| 8 | Closing evaluation, Part B (`final/trials.*`) | 18 real trials, all valid. Every twin verdict inconclusive at 8 pairs. Twin and production agreed on the index's benefit (0.11-0.16× vs 0.12-0.15×). The canary rolled back a do-nothing change once in three |
| 9 | Live LLM runs (`agent`) | Five runs on free-tier models. Two reached a proposal; both were left inconclusive by the twin |

Three browser tests also drive the running system end to end, including a harmful change (dropping an index a tenant
needs) that the canary undid by itself at 3.69× against a 1.15× limit.

---

## Part 8. What is shown and what is not

| Shown | Not shown |
|---|---|
| The whole loop runs on a real stack, in a browser | The twin gate approving a safe change (on the laptop it cannot) |
| The canary rolls back a harmful change by itself | The twin catching a *real* harmful change (none has been found yet) |
| A gate that resamples inside a replay gives false alarms; one that judges pairs does not | That the twin predicts production in general (it matched for one index) |
| With *injected* harm, the aggregate gate approves what the per-tenant gate refuses | Anything general about the LLM's proposals (two examples) |
| The canary also raises false alarms on low-traffic classes | Any rate with a useful confidence interval |

Two phrases to keep apart, because the whole project lives between them:
**"we did not see any harm"** and **"we showed there was none"**.

The main positive result (row 4) is *semi-synthetic*: the noise is real measurement noise, the harm was added by
arithmetic. It shows how the two decision rules behave. It does not show the twin reproduces production.

---

## Part 9. The LLM agent

File: `controlplane/app/proposers/agent.py`.

- The model gets **ten read-only tools** (tenants, objectives, latency, top queries, tenant load, query plan, table
  profile, settings, what-if index, history) and **one tool with an effect**, `propose_action`.
- `propose_action` takes one typed action. If it does not validate, the model is told exactly why and may try again.
- It has 14 turns. The task tells it so, and the last turn is announced.
- Every tool call is stored as a *trace* with the proposal and shown in the console.
- The proposal then goes through T0 to T3 like any other. The model cannot approve, apply, or run SQL.

It can run against an Anthropic model, a model served by Groq, or Gemini (`proposers/groq_client.py` translates the one
model call for the last two).

**What happened live (2026-10-12):**

| Run | Model | Outcome |
|---|---|---|
| 1 | gpt-oss-120b (Groq) | Investigated sensibly, ran out of turns, no proposal. The adapter was hiding earlier tool results from it, so it kept asking again |
| 2 | same | Cut off by the provider's daily quota |
| 3 | gpt-oss-20b (Groq) | Proposed an index for `t_mixed`. Its explanation read well but joined two different queries. The twin did not show even the target's benefit |
| 4 | gpt-oss-120b | Cut off by quota again |
| 5 | gemini-3.8-flash | In 98 seconds proposed the index the rules also find. Twin: benefit shown for the target, neighbours not shown unharmed, inconclusive |

Lessons: tell the model its turn budget; do not hide tool results to save tokens; and a fluent rationale is not
evidence. Verification, not the explanation, decided.

---

## Part 10. Security

| Concern | Mechanism |
|---|---|
| Tenants reading each other's data | A role per tenant, row-level security, no privileges on the partitions themselves |
| Tenants bypassing the pooler | The pooler admits tenant roles only |
| An organization seeing another's proposals | Row-level security in the control database; without a request context the API's role sees nothing |
| A member acting beyond their role | Checked in the API and again by the database |
| Tampering with the audit log | Written by triggers; cannot be updated, deleted or truncated |
| A proposer running arbitrary SQL | It can only emit typed actions; the executor writes the SQL |
| The engine doing too much in production | `dbpilot_executor` is not a superuser and may only do what the action space needs |
| A proposal skipping verification | The state machine is a database trigger |
| Passwords | Argon2id hashes the API's role cannot read |
| Secrets | In an untracked `.env`; nothing hard-coded |

Known limits: PostgreSQL has no privilege narrower than table ownership for building an index, so the executor could
in principle read rows; and a full threat model and an isolation test suite are planned work (Part 15).

---

## Part 11. Measuring properly: benchmark mode

This is the current work. Its protocol is `docs/evidence/bench_noise/protocol.md`, committed before any run. **No
measurement from it exists yet.**

**Why.** Three weaknesses stood in the way of a real finding: no scenario harmed a neighbour, most of a transaction's
time was not database time, and the only metric (class-level p95) hides per-query changes.

**What was built.**

- **Benchmark mode** (`docker-compose.bench.yml`, `bench/bench.env`): every container on named cores with a fixed
  memory limit and fixed settings. This makes the configuration repeatable. It does not make the laptop steady, which
  is why the noise is measured.
- **A pre-run checklist** in `scripts/run_benchmark.sh`: on AC power, sleep held off, no busy process outside Docker,
  idle host. A failed check aborts the run.
- **Workload variants.** `v0` the original; `v1` prepared statements; `v2` New-Order reading its items in one
  statement and inserting its lines in one (about 18 round trips instead of 36); `v3` the same from inside the
  pooler's network namespace.
- **New metrics.** p50/p95/p99 per tenant and class, per transaction type, and per *query template* (statements that
  share a query id); throughput; and the *DB-time share*: time executing in the database ÷ latency the client saw.
- **Three ways to run the twin's two arms:**
  - *sequential*: one after the other (the original);
  - *simultaneous*: both clones up on their own cores, replayed at the same moment by two processes, so whatever the
    machine does in that minute it does to both;
  - *interleaved*: the window cut into five blocks, each replayed on one arm then the other in a random order, so
    drift slower than a block falls on both.

**The noise floor.** For each metric, take the standard deviation s of ln(ratio) between *identical* conditions over 12
repetitions. The smallest effect 10 repetitions can detect with 80% power is `MDE10 = 0.995 × s`. An effect smaller
than that will be reported as **not shown**.

**How the twin procedure is chosen.** By a rule written before the data: the lower median s over the class-level keys;
simultaneous if the two are within 10% of each other. All three are reported.

**A real bug found on the way.** The first batched New-Order updated all stock rows in one statement. Under concurrent
orders it crashed PostgreSQL 18.6 backends with a segmentation fault. The workload now updates stock one row per
statement. See `docs/evidence/bench_noise/pg18_batched_update.md`.

---

## Part 12. Running it

Full instructions: [DEMO_GUIDE.md](DEMO_GUIDE.md). The short version, from the repository root:

```bash
docker compose up -d                                   # start (first time: add --build, then dp-seed and demo-seed)
docker compose --profile demo up -d demo-load          # demo traffic; wait two minutes
```

Open <http://localhost:5173> and sign in with `DEMO_ADMIN_EMAIL` and `DEMO_ADMIN_PASSWORD` from `.env`.

A demo in five clicks: Agent Console → **Run rule-based proposer** → open the index proposal → watch T0, T1, T2 →
as admin **Override and approve** → canary → **Roll back**.

Tests:

```bash
docker compose run --rm --no-deps api python -m pytest -q                 # control plane, gate, engine
docker compose run --rm dp-test                                           # data plane
docker compose run --rm --entrypoint python workload -m pytest -q tests   # workload
docker compose run --rm twin-test                                         # twin
docker compose run --rm e2e                                               # browser
```

Benchmark mode and its measurements:

```bash
bash scripts/run_benchmark.sh all      # about 2.5 hours, on AC power, with the machine otherwise idle
```

Stop:

```bash
docker compose --profile demo down     # keeps the data
```

---

## Part 13. Where everything is

```
core/dbpilot_core/     actions.py (typed actions, executor)   gate.py (the gate)   pglog.py (statement-log parser)
controlplane/
  app/                 main.py, routers/ (API)   engine.py (verify, canary)   collector.py   observe.py
                       planning.py (reading aids)   proposers/ (rules.py, agent.py, groq_client.py)
  migrations/          001-007: tenancy, audit, security, auth, telemetry, latency, proposals
  tests/
dataplane/             postgres/ (schema, tenancy, executor role)   pgbouncer/   seed/   tests/
twin/agent/            pg.py (clones)   replay.py   runner.py (arm procedures)   whatif.py   app.py
workload/              workload/ (driver, tpcc, olap)   evaluation/ (trial harness)   profiles/
web/                   src/ (React console)   e2e/ (browser tests)
tools/                 aa/ (never-run Linux A/A)   final/ (closing evaluation)   bench/ (Phase 1 metrics and noise)
scripts/               run_benchmark.sh   bench_host.ps1
bench/                 bench.env (fixed settings of benchmark mode)
docker-compose.yml     the stack           docker-compose.bench.yml   benchmark mode on top of it
docs/
  REPORT.md            the short closing report: what was measured and what was not
  PROJECT_REPORT.md    the long report with diagrams and references
  ARCHITECTURE.md      the design specification
  DECISION_LOG.md      each phase's decisions, alternatives and likely questions
  evidence/            raw outputs and the protocol each run was made under
```

---

## Part 14. Mistakes worth remembering

These are the most useful things to be able to talk about.

1. **No A/A test before the first result.** The gate was trusted until an experiment built only to catch it lying
   showed it did. An A/A test belongs first.
2. **The wrong unit of evidence.** Thousands of requests in one replay looked like a large sample. The independent
   observations were the replay pairs.
3. **Before/after is not a control.** A "slowdown" measured against the minutes before was the machine drifting.
4. **Traps that do nothing.** A scenario was assumed harmful without checking it changed anything; the setting never
   reached tenant queries.
5. **A metric that cannot see.** Class-level p95 is set by the slowest kind of operation in the class; a query can get
   many times faster or slower and leave it unchanged.
6. **A guard that fires on noise.** The canary judged 20-second windows with a handful of samples and rolled back a
   change that did nothing. The same mistake as the first gate, in another place.
7. **Capturing the pooler's own statements** as tenant transactions, which filled a class's samples with no-ops.
8. **A what-if that looked in the wrong place.** A tenant's index was judged across every tenant's partition and
   rejected.
9. **An adapter that hid context to save tokens** and made the model waste its turns asking again.
10. **A clever statement that crashed the server.** A batched update that passed its tests at light load and
    segfaulted PostgreSQL under contention.

---

## Part 15. What comes next

A seven-phase plan, each experiment pre-registered:

| Phase | Goal | State |
|---|---|---|
| 1 | A measurable environment and its noise floor | Machinery built; measurement pending |
| 2 | At least four realistic scenarios that really harm a neighbour, each with a harmless counterpart | Not started |
| 3 | The ablation: no verification, canary only, twin on the whole workload, twin per tenant, both | Not started |
| 4 | The LLM proposer at a meaningful sample size, and a prompt-injection test | Not started |
| 5 | Threat model, isolation test suite, tamper-evident audit log | Not started |
| 6 | CI, one-command demo, a recording | Not started |
| 7 | README and a workshop-style paper | Not started |

The finding hoped for: "per-tenant twin gating catches X of Y neighbour-harming changes that whole-workload gating
misses, at a cost of Z". If the data do not support it, the report will say so.

---

## Part 16. A study plan

**Day 1: the idea and the system (3 hours)**
1. Parts 1 to 4 of this guide.
2. Run the demo (Part 12) and click one proposal through.
3. Read `core/dbpilot_core/actions.py`, then `controlplane/migrations/007_proposals.sql`.

**Day 2: the loop and the gate (3 hours)**
1. `controlplane/app/engine.py`: `verify`, then `canary`.
2. Part 6, then `core/dbpilot_core/gate.py`: `compare`, `decide`. Redo the worked example by hand.
3. `twin/agent/runner.py` and `pg.py`.

**Day 3: the evidence (3 hours)**
1. Part 7 and Part 8.
2. `docs/REPORT.md`, then `docs/evidence/README.md` and the `e1` and `final` folders.
3. Part 14. Practise telling each mistake in two sentences.

**Day 4: the rest**
1. Parts 9 to 11; `proposers/agent.py`; `docs/evidence/bench_noise/protocol.md`; `docs/DECISION_LOG.md`.
2. [DEFENSE_QUESTIONS.md](DEFENSE_QUESTIONS.md): cover the answers and say each aloud.

Other documents, for depth: [KEY_CONCEPTS.md](KEY_CONCEPTS.md) (a dictionary),
[ARCHITECTURE_WALKTHROUGH.md](ARCHITECTURE_WALKTHROUGH.md) (which function calls which),
[LEARNING_GUIDE.md](LEARNING_GUIDE.md) (each technology explained), [PROJECT_REPORT.md](PROJECT_REPORT.md)
(diagrams and references).

---

## Glossary

| Term | Meaning here |
|---|---|
| A/A test | A comparison of two identical arms; measures the method's own noise |
| Action space | The closed list of nine kinds of change a proposer may suggest |
| Aggregate gate | Judges only the pooled workload; the comparison point |
| Arm | One of the two clones in a comparison: control (unchanged) or treatment (with the change) |
| Baseline | Each tenant's p95 just before a canary applies a change |
| Benchmark mode | The stack with every container's cores, memory and settings fixed |
| Canary | Applying a change in production under watch, with automatic rollback |
| Class | OLTP or OLAP traffic of one tenant, e.g. `t_mixed/OLAP` |
| Contract | The largest ratio to baseline a tenant may show during a canary |
| DB-time share | Time executing in the database ÷ latency the client saw |
| Digital twin | A real PostgreSQL copy of production, replayed with captured traffic |
| Executor | The code that turns a typed action into SQL and its inverse |
| Gate | The rule that turns twin measurements into APPROVE, REJECT or INCONCLUSIVE |
| Inverse | The SQL that undoes an applied action, stored before it is applied |
| Look | One more replay pair added to a verdict |
| MDE10 | The smallest effect 10 repetitions detect with 80% power; the noise floor |
| Non-inferiority | Shown to be not worse by more than a margin |
| Open loop | Load whose arrival times do not depend on how fast the system answers |
| Outcome ledger | The view joining proposal, twin prediction and production result |
| Per-tenant gate | Judges every tenant and class separately; DBPilot's gate |
| Pre-registration | Fixing the analysis before the data exist |
| Query template | Statements sharing a `pg_stat_statements` query id |
| Replay pair | One control and one treatment replay of the same captured window |
| Semi-synthetic | Real measurements with an artificial effect added by arithmetic |
| Tenant | One customer in the shared database |
| What-if | Asking the planner about a hypothetical index without building it |
