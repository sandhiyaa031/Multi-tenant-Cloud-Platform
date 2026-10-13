# Phase 1 protocol: a measurable environment and its noise floor

Written and committed before any of it is run. If something here turns out to be unworkable, the change is made in
this file in a separate commit, with the reason, before the affected part is run again; results obtained under the old
text are kept and labelled.

Everything is measured on one laptop that is known not to be a controlled machine (`docs/evidence/p01h`: its speed
varies by 5-24% with CPU frequency). Pinning and memory limits make the configuration repeatable. They do not make the
machine steady. The purpose of this protocol is to measure how unsteady it is, in the units later phases will use, and
to choose the measurement procedure that suffers least from it.

## Benchmark mode

- Compose override `docker-compose.bench.yml` on top of the laptop profile: fixed CPU sets and memory limits for every
  container, one fixed PostgreSQL configuration, dataset scale 0.3 (as in every earlier measurement).
- Every run records: git commit, image ids, the full `pg_settings` of the primary, the compose resource limits, power
  source at start and end, and the System log's power-source events (Kernel-Power 105) for the period.
- Workload: `workload/profiles/eval.json` rates at `EVAL_RATE_SCALE=0.5`, as in the closing evaluation, unless Part 0
  adopts another workload variant; then that variant at the same rates.
- A measurement starts after a warm-up of 150 s. Windows are fixed in advance and never extended or cut on sight of
  the numbers.

## Metrics

For every tenant and class (as before), and now also for every query template (statements that share a
`pg_stat_statements` query id) and every transaction type of the driver:

- latency p50, p95, p99: server side from the statement log (duration per statement, grouped by query id), client side
  from the driver's own timing per transaction type;
- throughput: completed transactions per second per tenant and class;
- from `pg_stat_statements`, per template: calls, mean time, shared blocks read and hit, WAL bytes. It has no
  percentiles; none are derived from it.

Class-level p95 is kept and reported beside the new metrics.

## Part 0. Where the time goes (a measurement, not a comparison of treatments)

Question: what share of a transaction's client-side latency is spent executing statements in the database?

- DB-time share of a class = sum of server-side statement durations of its transactions / sum of their client-side
  latencies, over one 180 s window after warm-up. Reported per tenant and class and per transaction type.
- Workload variants, each measured once with the same rates and seed:
  - V0: the driver as it is (simple protocol, 30 to 40 round trips per New-Order, through the pooler over the bridge).
  - V1: V0 with protocol-level prepared statements.
  - V2: V1 with New-Order's order lines written set-based (one statement for the stock updates, one for the line
    inserts) and independent statements of a transaction sent in one pipeline where the pooler supports it.
  - V3: V2 with the driver in the pooler's network namespace (loopback).
- V2 is admissible only if it is a workload a real application would send: batched line items are how TPC-C kits and
  ORMs with batching behave. It must pass the workload tests (same rows written, same consistency rules).
- Adopted for all later measurements: the highest-numbered variant that passes the workload tests, runs without
  errors through the pooler, and is replayed by the twin with at most 1% replay errors. V0 is kept as a profile.
- Reported whatever the outcome: the DB-time share of every variant, including if it stays low.

## Part 1. Noise floor in production (A/A)

Question: with nothing changed, how much does a metric differ between adjacent periods?

- One continuous run of the adopted workload: 150 s warm-up, then 12 repetitions of three consecutive 60 s segments
  (before, live, after). Nothing is changed at any point.
- Per repetition and metric: r = live / mean(before, after), the same bracketed ratio the trial harness uses.
- Per key and metric: the standard deviation s of ln r over the repetitions, and the mean with its 95% t interval.

## Part 2. Noise floor on the twin (A/A), and how the arms should be run

Question: with identical arms, which way of running the two arms gives ratios that vary least?

- Three procedures, each a replay of the same captured 55 s window against two identical clones:
  - sequential (the procedure used so far): one arm after the other, order alternating between pairs;
  - simultaneous: both clones up, each on its own CPU set, replayed at the same time by two replayer processes on
    their own cores; which CPU set holds the arm labelled "treatment" alternates between pairs;
  - interleaved: both clones up; the window is cut into 5 blocks of 11 s; for each block one arm is replayed, then the
    other, in an order drawn at random per block from a seeded generator (seed recorded); each clone receives its
    blocks in their original order.
- 12 rounds. A round is one pair by each procedure, in the order sequential, simultaneous, interleaved in round 1 and
  rotated by one position each round, so that slow drift over the session falls on all three alike.
- Production runs the adopted workload throughout, as the twin needs captured workload to replay.
- Per pair and key: ln(treatment metric / control metric), for class-level p95 and for per-template p95 of every
  template with at least 30 executions per arm.
- Per procedure and key: s, the standard deviation of that quantity over the pairs.

**Choice of procedure, fixed now.** Each of simultaneous and interleaved is summarised by the median over the
class-level keys of s. The one with the lower median is adopted for all later twin measurements. If the two medians
are within 10% of each other, simultaneous is adopted, because it takes about half the time per pair. Sequential is
reported for comparison and is adopted only if both new procedures have a higher median than it. All three are
reported in full whatever is chosen. The gate's decision rule is not changed by this protocol.

## What "the noise floor" means from here on

For a key and metric with standard deviation s of ln ratio between identical conditions, the smallest effect that
10 repetitions detect with 80% power at a two-sided 5% level is

    MDE10 = (t(0.975, 9) + t(0.80, 9)) * s / sqrt(10) = 0.995 * s,   reported as exp(MDE10) - 1 in percent.

That number, per key and metric, is the noise floor. In later phases an effect is **shown** only if both hold: the 95%
t interval of its mean ln ratio over at least 10 repetitions excludes 0, and its size is at least MDE10. Anything else
is reported as **not shown**. s is an estimate from 12 repetitions and is itself uncertain; its 95% interval is
reported with it.

## Validity and stopping

- A repetition or pair is invalid, and listed as such, if: the power source changed during it; the driver's error rate
  or the twin's replay errors exceed 1%; a container restarted; or telemetry has a gap in it.
- The checklist of `scripts/run_benchmark.sh` must pass before a run starts: AC power connected, system sleep disabled
  for the run, CPU load from other processes below 10% of one core averaged over 30 s, Docker memory and CPU counts as
  recorded in this protocol's run log. A failed check aborts the run.
- The number of repetitions is fixed at 12 per part. Invalid ones are repeated at the end of the session, at most 4
  per part. Nothing is stopped early or extended because of what the numbers look like. If fewer than 10 valid
  repetitions exist after the extra ones, the part is reported as incomplete and run again from the start on another
  day, and both sessions are kept.

## What is reported

- Part 0: DB-time share per variant, per class and transaction type; which variant was adopted and why.
- Parts 1 and 2: for every key and metric, s, its interval, and MDE10; for Part 2 per procedure, and the choice.
- Excluded repetitions and why.
- A statement, per scenario family planned for Phase 2, of the smallest harm that could be shown at these floors.

## What this cannot show

- That the laptop is a controlled environment. It is not.
- That the floors hold on another day. They are re-measured at the start of each later measurement session by the same
  script, and a session whose floor differs from this one by more than a factor of 1.5 for any class-level key is
  flagged.
