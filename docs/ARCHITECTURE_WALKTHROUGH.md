# Architecture walkthrough

A map of the code: what each component is, which file holds it, and what calls what. Open the files as you read. Paths
are relative to the repository root. Function and table names are the ones in the code at the time of writing.

## 1. The services

Everything runs as containers defined in `docker-compose.yml`.

| Service | Code | What it does | Talks to |
|---|---|---|---|
| `web` | `web/` (React, built and served by nginx) | The console in the browser | `api`, through nginx's `/api/` proxy (`web/nginx.conf`) |
| `api` | `controlplane/app/main.py`, `routers/` | REST API: sign-in, resources, telemetry, proposals | `control-db`; `twin` (plans, what-if); data plane (table profiles, settings) |
| `control-db` | `controlplane/migrations/*.sql` | The control plane's own PostgreSQL: users, tenants, telemetry, proposals, audit | — |
| `migrate` | `controlplane/migrate.py` | Applies the migrations once at start | `control-db` |
| `collector` | `controlplane/app/collector.py` | Reads statistics and the statement log, writes telemetry | `dp-primary` (read-only monitor role), the shared log volume, `control-db` |
| `engine` | `controlplane/app/engine.py` | Verifies proposals, runs canaries, rolls back | `control-db`, `twin`, `dp-primary` (executor role), `pgbouncer` (admin) |
| `dp-primary` | `dataplane/postgres/` | Production PostgreSQL | — |
| `dp-replica` | `dataplane/postgres/replica-entrypoint.sh` | Streaming replica | `dp-primary` |
| `pgbouncer` | `dataplane/pgbouncer/` | The only door tenants use | `dp-primary` |
| `twin` | `twin/agent/` | Twin node agent: clones, replay, what-if | `dp-primary` (replication), the shared log volume |
| `dp-seed`, `demo-seed` | `dataplane/seed/seed.sql`, `controlplane/demo_seed.py` | Load four tenants; register them as a demo organization | — |
| `demo-load`, `workload` | `workload/workload/` | Generate tenant traffic | `pgbouncer` |
| `evaluation` | `workload/evaluation/` | Trial harness for experiments | `api`, `pgbouncer` |
| `e2e`, `dp-test`, `twin-test` | `web/e2e/`, `dataplane/tests/`, `twin/tests/` | Test runners | the running stack, or their own miniature one |

Shared code used by several services lives in `core/dbpilot_core/`:

| File | Used by | Contents |
|---|---|---|
| `actions.py` | api, engine, twin | The typed action space; `plan()` builds apply and inverse SQL; `run()` executes it |
| `gate.py` | engine, analysis scripts | `pool()`, `compare()`, `decide()`: the safety gate |
| `pglog.py` | collector, twin | Parses PostgreSQL's JSON statement log into transactions per tenant |

One shared volume matters: `dp-logs`. The primary writes its statement log there; the collector reads it for latency
percentiles; the twin reads the same files to get the transactions it replays.

## 2. The frontend

| File | Contents |
|---|---|
| `web/src/main.tsx`, `App.tsx` | Routes; the signed-in shell (`Shell`) with sidebar, cluster picker, organization switcher, collector-status badge; redirect to sign-in when there is no session |
| `web/src/api.ts` | `fetch` wrapper. Keeps the token in `localStorage`, sends it as `Authorization: Bearer`, and on a 401 clears it and returns to sign-in with a notice |
| `web/src/pages/Public.tsx` | Landing, Login, Signup, Forgot, Reset |
| `web/src/pages/Observe.tsx` | Overview, Tenants, Workloads, Query Intelligence, Database Health |
| `web/src/pages/Optimise.tsx` | Agent Console, Recommendations, the proposal page, Digital Twin Lab, Verification, Canary Deployments, Experiments |
| `web/src/pages/Admin.tsx` | Audit History, Settings (members, invitations, clusters) |
| `web/src/ui.tsx` | Shared pieces: `EffectsPlot` (the interval chart), `Stepper`, `ErrorBoundary` |

The proposal page (`ProposalPage` in `Optimise.tsx`, route `/app/recommendations/:id`) is the centre of the demo. It
polls `GET /proposals/{id}` and renders the lifecycle stepper, the verification steps (`Steps`), the twin result
(`TwinRun`) and the canary observations (`CanaryView`).

## 3. A request, end to end

Example: the Overview page asks for latency.

1. `Observe.tsx` calls `api("/clusters/{id}/latency")` in `api.ts`.
2. nginx forwards `/api/...` to the `api` service.
3. `controlplane/app/deps.py: get_principal` verifies the token's signature and expiry (`security.py`), then opens a
   database transaction with `db.py: org_tx`, which sets `app.user_id` and `app.org_id` for that transaction only.
4. `require_role("VIEWER")` reads the member's role from `cp.memberships` (not from the token) and checks it.
5. The handler in `routers/telemetry.py` runs its query. Row-level security policies in
   `migrations/003_security.sql` filter rows to the caller's organization: a second check, made by the database.
6. The transaction commits and the identity is gone.

So authorization is enforced twice: in the API, and again by PostgreSQL.

## 4. Where telemetry comes from

```
dp-primary ── pg_stat_statements, pg_stat_database ──▶ collector ──▶ cp.query_stats, cp.instance_stats
dp-primary ── JSON statement log (dp-logs volume) ───▶ collector ──▶ cp.latency_stats
                                                  └──▶ twin (replay)
```

`Collector.collect_once` (`collector.py`) runs every `COLLECT_INTERVAL_S` seconds. `_collect_cluster` snapshots the
cumulative counters and stores the difference from the previous snapshot (`counter_delta`). `_collect_latency` uses
`pglog.LogTailer` to read new log lines, `pglog.Assembler` to group statements into transactions per tenant and class,
and `latency_rows` to compute p50, p95 and p99 per window. The API's telemetry endpoints and the canary both read these
tables.

`controlplane/app/observe.py: Observer` is the read-only view of one cluster that both the API's insight endpoints and
the proposers use: `slo_status`, `latency`, `top_queries`, `tenant_load`, `history`, `table_profile`, `settings`,
`explain`, `whatif_index`. The last two call the twin node agent over HTTP.

## 5. The life of a proposal

The states and the legal moves between them are rows in `cp.proposal_transitions`, enforced by the trigger
`cp.proposal_guard` (`migrations/007_proposals.sql`). No code can skip a state, and the action cannot be edited after it
is proposed.

```
PROPOSED → VERIFYING → REJECTED | INCONCLUSIVE | AWAITING_APPROVAL | APPROVED | ADVISORY
AWAITING_APPROVAL → APPROVED (operator) | REJECTED
INCONCLUSIVE      → APPROVED (admin override) | REJECTED
APPROVED → CANARY → APPLIED | ROLLED_BACK
APPLIED  → ROLLBACK_REQUESTED → ROLLED_BACK
```

### Step 1: a proposal is created

Three ways, all in `controlplane/app/routers/proposals.py`:

- By hand: `POST /clusters/{id}/proposals` → `create_proposal` → `insert_proposal`, which validates the action with
  `actions.parse_action` and inserts a row in state `PROPOSED`.
- Rule-based proposer: `POST /clusters/{id}/diagnose` with `source: "rule"` → `_diagnose` → `rules.propose(observer)`
  (`proposers/rules.py`). Its rules are `_spills`, `_missing_indexes`, `_stale_statistics`, `_bursts`. For example
  `_missing_indexes` takes each tenant's expensive `SELECT`s, finds equality filters on unindexed columns, and calls
  `Observer.whatif_index`; if the planner estimates the cost at least halves, it emits a `create_index` action.
- LLM agent: the same endpoint with `source: "agent"` → `agent.propose(observer)` (`proposers/agent.py`). The model is
  given the tool list `TOOLS`; each tool call is executed by `_call` against the `Observer`; the loop ends when the model
  calls `propose_action`. Every call is stored as a trace with the proposal. Without `ANTHROPIC_API_KEY` the endpoint
  answers 503.

The API only writes the row. It never verifies or applies anything.

### Step 2: the engine picks it up

`engine.py: main` loops over `Engine.work_once`, which looks for work in this order: a proposal in `APPROVED` (start its
canary), a `ROLLBACK_REQUESTED` (undo it), a `PROPOSED` (verify it). `claim` moves the state with
`FOR UPDATE SKIP LOCKED`, so two engines could not take the same proposal.

### Step 3: `Engine.verify`

| Tier | Code | What happens |
|---|---|---|
| T0 | `t0_static` | Valid action, known tenant, not a duplicate of an applied action, worst-case memory arithmetic. Advisory actions stop here as `ADVISORY` |
| — | | If the proposal's verification mode is `canary_only` or `none`, T1 and T2 are recorded as skipped and it goes straight to `APPROVED` |
| T1 | `POST /whatif` on the twin → `twin/agent/whatif.py: index_whatif` | For `create_index` only. The observed queries on that table are planned with and without a hypothetical index. If none would use it: `REJECTED` |
| T2 | `Engine.twin_run` → `POST /runs` on the twin, polled with `GET /runs/{id}` | One replay pair per look, up to `TWIN_MAX_LOOKS`. After each pair the samples are added with `gate.pool` and `Engine.judge` calls `gate.decide`. Arm order alternates: the treatment arm runs first on even looks |

Each tier writes a row with `Engine.step` into `cp.verification_steps`; the twin's pooled result goes into
`cp.twin_runs`. The final state is `REJECTED`, `INCONCLUSIVE`, `AWAITING_APPROVAL`, or `APPROVED` if the proposal was
created with auto-approve.

### Step 4: what the twin does for one run

`twin/agent/app.py` is a small FastAPI service. `start_run` hands the job to `twin/agent/runner.py: run`:

1. `pg.freeze_base()` (`twin/agent/pg.py`): pause replay on the delayed standby, record its position, copy its data
   directory to a base copy, resume. The base is the database at a known past moment `T0`.
2. `pglog.read_window(LOG_DIR, T0, T0 + window)`: the transactions production actually ran in that window.
3. For each arm, `_arm`: `pg.fresh_work()` makes a working copy of the base and starts it as a standalone database
   stopped exactly at the frozen position; for the treatment arm `actions.plan` and `actions.run` apply the change (the
   same code the engine later uses on production); `pg_prewarm` loads every relation so both arms start equally warm; a
   checkpoint; then `replay.replay(...)` runs each transaction as its tenant at its original offset and records how long
   it took.
4. The result returns the latency samples per `tenant/class` for each arm, WAL bytes, storage growth and errors.

### Step 5: the gate

`core/dbpilot_core/gate.py`.

- `compare(control, treatment, policy, alpha)` handles one tenant and class. For every replay pair it computes
  treatment p95 ÷ control p95. With one pair it returns `ONE_PAIR` (no interval). With more, it takes the logarithm of
  each ratio and builds a Student's t interval (`t_quantile`) around their mean; the result is the geometric-mean ratio
  with `lo` and `hi`.
- `decide(...)` first enforces absolute budgets (storage, write volume). In per-tenant mode it then classifies each key:
  - `HARMED`: the whole interval is above the harm limit (1.05) **and** a pair above the limit exists in both arm
    orders (`_in_both_orders`); or an SLO that control met is broken in both orders;
  - `SAFE`: the whole interval is at or below 1.05;
  - `BENEFITS`: a target key whose whole interval is below 0.90;
  - otherwise `UNCERTAIN`.
- Decision: any `HARMED` → `REJECT`; else any `UNCERTAIN` → `INCONCLUSIVE`; else a benefit shown → `APPROVE`; else no
  benefit possible → `REJECT`.
- `_decide_aggregate` does the same on the pooled workload with no per-tenant view. `Engine.judge` runs both modes on
  the same measurements and stores the other one as `shadow`, for comparison only.
- The verdict also carries a `contract`: per key, the largest production ratio still consistent with what the twin saw.

### Step 6: a person decides (when needed)

`POST /proposals/{id}/approve` and `/reject` → `_decide` in `routers/proposals.py`. An operator can approve a proposal
that passed (`AWAITING_APPROVAL`); only an admin can override an `INCONCLUSIVE` one. The reason typed is stored and
appears in the audit log, which is written by database triggers (`migrations/002_audit.sql`).

### Step 7: `Engine.canary`

1. **Baseline**: mean p95 per tenant and class over the last collector windows (`latency_windows`). No baseline means no
   traffic or no collector, and the proposal goes to `FAILED`.
2. **Contract**: the twin's contract where there is one, otherwise 1 + 0.05 + 0.10 = 1.15.
3. **Apply**: `actions.plan` against production as the least-privilege role `dbpilot_executor`, then `actions.run`. The
   applied statements and the inverse are stored in `cp.canaries` before anything is observed. `canary_stages` splits an
   index for every tenant into one partition first, then the rest.
4. **Watch**: for each new collector window, `window_breaches` compares p95 ÷ baseline with the contract;
   `guard_breaches` adds deadlocks and replica lag.
5. **Decide**: `rollback_reason` says roll back when two of the last three windows breached, or telemetry was missing
   for two windows. Then `actions.run(plan.inverse)` and state `ROLLED_BACK`. Otherwise state `APPLIED`.

Known limits: the canary's latency is measured inside the database, so harm that is only waiting at the pooler (a
concurrency cap) is invisible to it; and a window is judged once it holds `canary_min_txns` (5) transactions of a class,
which is too few for a steady p95. In the closing trials that produced one false rollback of a do-nothing change.

### Step 8: rollback on request, and after a crash

`POST /proposals/{id}/rollback` moves `APPLIED` to `ROLLBACK_REQUESTED`; `Engine.manual_rollback` runs the stored
inverse. `Engine.recover`, run at start-up, rolls back any proposal found in `CANARY`: a change nobody was watching is
treated as unsafe.

### Reading aids (added after the evaluation; they decide nothing)

`controlplane/app/planning.py` holds two pure functions. `twin_planning` is called by `get_proposal`
(`routers/proposals.py`) and adds, for each tenant and class of a stored verdict, an estimate of the replay pairs needed
to show it unharmed (`pairs_needed` solves the gate's interval condition for the number of pairs). 
`canary_false_alarms` is called by `GET /clusters/{id}/canary-noise` (`routers/insight.py`): it slides the canary's own
`window_breaches` and `rollback_reason` over recent rows of `cp.latency_stats`, skipping any stretch that contains a
canary start, a canary finish or a rollback. The console shows them in `TwinRun` and `CanaryNoise` (`Optimise.tsx`);
`downloadEvidence` on the proposal page saves the whole proposal record as JSON.

### Step 9: the outcome ledger

The view `cp.outcome_ledger` joins each proposal with its latest twin verdict and its canary result
(`production_ratios`). `GET /clusters/{id}/ledger` and `/experiments` (`routers/insight.py`) serve it to the Experiments
page. `Engine.calibration` also reads it: once eight twin-versus-production comparisons exist for a kind of action, the
canary tolerance for that kind becomes the 90th percentile of the twin's past error (`gate.calibrated_tolerance`).

## 6. The data plane

| File | Contents |
|---|---|
| `dataplane/postgres/initdb/01_roles.sh` | Service roles |
| `dataplane/postgres/initdb/02_schema.sql` | The `ch` schema, partitions per tenant, row-level security, `ch.tenant_map` |
| `dataplane/postgres/initdb/03_loader.sql` | Functions that create a tenant and load its data |
| `dataplane/postgres/executor.sql` | The `dbpilot_executor` role: what the engine may and may not do in production |
| `dataplane/pgbouncer/entrypoint.sh` | Pooler configuration; tenant-only login |

## 7. The workload and the experiment harness

`workload/workload/driver.py: run` starts one `stream` per tenant and class from a profile in `workload/profiles/`.
Arrivals follow a schedule that does not depend on how fast the database answers (an *open loop*), and latency is
measured from the scheduled time. `tpcc.py` holds the five transactions, `olap.py` the analytical queries.

`workload/evaluation/__main__.py: trial` is one experiment trial: start the scenario's workload, submit its action
through the public API with a configuration's verification settings, follow the proposal to its final state, undo it,
and record client-side latency before, during and after. Scenarios and configurations are in
`workload/evaluation/scenarios.json`.

## 8. Tests, and what each one stands on

| Suite | Location | Runs against |
|---|---|---|
| Control plane, gate, engine | `controlplane/tests/`, `core/tests/` | The real control database; the twin replaced by a stand-in in `test_engine_verify.py` |
| Data plane | `dataplane/tests/test_dataplane.py` | The real primary, replica and pooler |
| Workload | `workload/tests/test_workload.py` | The real data plane through the pooler |
| Twin | `twin/tests/` | A miniature production built inside the test container |
| Browser | `web/e2e/*.spec.ts` | The whole running stack with the demo workload |

## 9. Experiment scripts and evidence

| Location | Contents |
|---|---|
| `tools/aa/` | Protocol and runner for an A/A validation on a controlled Linux machine. **Never run** (no machine) |
| `tools/final/` | Closing evaluation: `protocol.md`, `power.py` (injected effects), `run_trials.sh`, `trials_report.py` |
| `docs/evidence/` | Raw outputs of every diagnostic and the closing evaluation, with the protocol each ran under |
| `results/` | Working output directory. Not tracked by git |
