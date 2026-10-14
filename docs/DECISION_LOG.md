# Decision log

One entry per phase of the work that follows the closing report. Each says what changed, why, what was considered and
not done, and what a reviewer is likely to ask. Numbers quoted here are from `docs/evidence/`; if an entry and the
evidence disagree, the evidence is right.

---

## Phase 1: make the environment measurable (in progress)

Protocol: [evidence/bench_noise/protocol.md](evidence/bench_noise/protocol.md), committed before anything was run.
**No Phase 1 measurement has been taken yet.** What exists is the machinery, checked with short smoke runs whose
numbers are not results. The measurements wait for a machine with nothing else running on it.

### What changed

| Change | Where | Why |
|---|---|---|
| Benchmark mode | `docker-compose.bench.yml`, `bench/bench.env` | Every container on named cores with a fixed memory limit and fixed settings, so two sessions run the same configuration. The control plane had no pinning before and could wander onto data-plane cores |
| One script for every measurement | `scripts/run_benchmark.sh`, `scripts/bench_host.ps1` | Stages `up`, `part0`, `part1`, `part2`, `report`. A pre-run checklist aborts the run unless the laptop is on AC, sleep is held off, and the host is idle. The power source is logged every 5 s so a repetition during which the charger dropped can be excluded |
| Workload variants | `workload/workload/driver.py`, `tpcc.py` | `v0` is the driver as it was. `v1` adds protocol-level prepared statements. `v2` also reads an order's items in one statement and inserts its lines in one (about 18 round trips per New-Order instead of about 36). `v3` runs `v2` from inside the pooler's network namespace |
| Per-operation recording | `--ops` on the driver | Every completed operation with its scheduled time and latency, so percentiles per transaction type and the DB-time share can be computed afterwards |
| New metrics | `tools/bench/metrics.py` | p50/p95/p99 per tenant and class, per driver operation, and per query template from the statement log; throughput; the share of client latency spent executing in the database. Class-level p95 is kept beside them |
| Three ways to run the twin's arms | `twin/agent/runner.py`, `replay.py`, `pg.py` | `sequential` (as before), `simultaneous` (both clones up on their own cores, replayed at the same moment by two processes) and `interleaved` (five blocks, each replayed on one arm then the other in a seeded random order). The A/A measurement will choose between them |
| Per-template timing on the twin | `twin/agent/replay.py` | Each replayed statement is timed under the query id production logged for it |
| Noise-floor arithmetic | `tools/bench/noise_prod.py`, `noise_twin.py` | For each metric: the standard deviation of ln(ratio) between identical conditions, its interval, and MDE10, the smallest effect 10 repetitions detect with 80% power. An effect below MDE10 is "not shown" |

Tests added: workload 24 (was 20), twin 54 (was 48), `tools/bench` 6. All earlier tests pass.

### Decisions, with what was rejected

**1. The noise floor is defined as a detectable effect, not as a percentile of observed ratios.**
With 12 repetitions the 95th percentile of |ln ratio| is nearly the maximum, a statistic that swings wildly between
sessions. The standard deviation uses every repetition, and MDE10 = 0.995 × s turns it into the question that matters
later: how big must an effect be for 10 repetitions to find it? *Rejected:* "largest A/A ratio seen" (unstable), and a
fixed 5% threshold (a guess).

**2. `pg_stat_statements` is not the source of per-template percentiles.**
It stores only mean, min, max and standard deviation. Percentiles come from the statement log, which already carries
a duration and a query id per statement, and on the twin from timing each replayed statement. *Rejected:* estimating
p95 from mean and standard deviation (assumes a distribution latencies do not have).

**3. Three procedures for the twin's arms, and a rule for choosing fixed in advance.**
The A/A evidence showed tenants drifting together between sequential arms, which points at the machine changing speed
between the two replays. Running the arms at the same time, or in short alternating blocks, attacks that directly. The
protocol says which wins: the lower median standard deviation over the class-level keys; simultaneous if they are
within 10%, because it takes half the time. *Rejected:* choosing after looking (the forking-paths problem), and
changing the gate to tolerate more noise (the gate's rule is not touched in this phase).

**4. The twin's `treatment_first` flag was kept and given a second meaning.**
In sequential mode it says which arm runs first. In the other two it says which clone slot (and so which CPU set and
data directory) the treatment gets. The gate's rule "harm must be seen in both orders" therefore still means something:
harm must be seen with the treatment on either slot. No gate code changed.

**5. New-Order's stock updates are not batched.**
The first batched form (a CTE that locks the rows in order, joined to the UPDATE) crashed PostgreSQL 18.6 backends
with a segmentation fault under concurrent orders, about every 18 seconds in a stress run, and left one backend
spinning that could not be terminated ([evidence/bench_noise/pg18_batched_update.md](evidence/bench_noise/pg18_batched_update.md)).
A plain `UPDATE ... FROM unnest(...)` did not crash but once returned fewer rows than it should have. The adopted form
updates stock one row per statement by primary key, the path every TPC-C kit uses, and batches only the item read and
the line insert. *Rejected:* keeping a statement that works at light load and fails under contention; and stored
procedures, which would give one round trip per order but hide the statements from the statement log, so the
per-template metrics this phase adds would see one opaque call.

**6. The protocol was amended three times before any measurement, each time in its own commit.**
The idle-host threshold (the first text could not be met or measured: the idle stack alone uses about 6% of the
machine); dropping pipelining from V2 (nothing left to pipeline once lines are batched); and the stock-update change
above. Amending a protocol before data exists is allowed; amending it after is not, and the file says which is which.

**7. Sleep is held off by a per-process request, not by changing a power setting.**
`bench_host.ps1` calls `SetThreadExecutionState` for as long as the run lasts. Nothing in Windows' configuration is
modified, and the request dies with the process.

### What the smoke runs suggest (not results)

Thirty-second windows with another program using a full core, so these are observations to check, nothing more:

- Batching halved New-Order's client latency (about 17 ms to 8 ms), but the share of latency spent executing in the
  database did not rise; it stayed between a quarter and a third for OLTP in every variant. The time per round trip
  (about 0.7 ms) is not network: moving the driver onto loopback changed little. If the real measurement confirms it,
  the honest conclusion of Part 0 is that client-side latency on this machine is dominated by per-round-trip overhead
  whatever the driver does, and that server-side and per-template timing are where a change in the database has to be
  looked for.
- All four variants ran through the pooler without errors, and the twin replayed the `v3` capture with 0 errors in
  2,344 transactions.

### Questions a reviewer might ask

- *Why not just pin CPUs and call the environment controlled?* Pinning fixes which cores a container may use. It does
  not fix how fast those cores run: the laptop's speed followed CPU frequency by 5-24% in an earlier diagnostic, and a
  cgroup cannot see that. So the noise is measured instead of assumed away.
- *Isn't choosing the best of three twin procedures a way to cherry-pick?* It would be if the choice were made on the
  treatment data. It is made on A/A data, where the true effect is zero, by a rule written before the data existed,
  and all three are reported.
- *Why 10 repetitions and 80% power?* Ten is the minimum the brief asked for; 80% is the conventional power. The
  formula is in the protocol, so a reader can recompute the floor for any other choice.
- *Does batching make the workload unrealistic?* Reading several items in one statement and inserting several lines in
  one is what an application with a batching ORM does. The rows written are the same; a test checks it.
- *You found a PostgreSQL crash. Did you report it?* Not yet. It is documented with the statement, the conditions and
  the server version. It has not been reproduced outside this stack or bisected, which should come first.
- *What happens if the noise floor comes out at 5-12% again?* Then it is reported as that, and every Phase 2 scenario
  whose expected harm is smaller is listed as not showable on this machine before Phase 2 starts.

## Review preparation (2026-10-14)

Goal: make the console, the API and the databases demonstrable end to end for the DBMS review, without new research.

- **Organizations** are now a managed resource (create, list, rename, archive), distinct from data-plane tenants. Archive,
  not delete, because the audit log references organizations and is immutable. Cluster retire added, refused while a
  change is in flight or applied.
- **Guided demo** page walks the twelve steps on live endpoints only. Gate thresholds and algorithm were not touched.
- **Bug found by the full test run:** the missing-index rule asked the planner about the window's top 100 queries; after
  heavy test traffic the triggering query was pushed out, so the rule proposed nothing. The rule now passes its own query.
  Regression test added.
- **Results (this laptop, 2026-10-14):** control plane 234 passed; data plane 36; twin 54; workload 24 (two row-count tests
  fail while demo traffic is running and pass with it paused); browser: demo, org, consistency, states, guided, audit,
  rollback and Route A pass; the twin browser spec failed once after 22 s (cause not established) and completed the full
  twin, decision, apply and rollback cycle on rerun.
- **Not done:** live LLM step (Gemini returned 503 that day; earlier live runs are in `docs/evidence/agent/`); no real
  twin APPROVE exists, so APPROVE wording is tested on the verdict function only; no hosted deployment was created.
