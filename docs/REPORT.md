# DBPilot: what was built, what was measured, what was not

This is the closing report of the project. It separates four things that are easy to blur: what exists and is tested,
what was observed on the development laptop, what a semi-synthetic analysis says about the decision rule, and what has
not been shown at all. Every number here comes from one laptop that is not a controlled environment; where that limits a
statement, the statement says so. Raw outputs and the protocols they were run under are in
[docs/evidence](evidence/README.md).

## 1. The idea

Several tenants share one PostgreSQL instance. A change that helps one of them (an index, more memory, more parallel
workers) can hurt another, and judged on the whole workload it can still look like a win. DBPilot lets a proposer (fixed
rules or an LLM agent) suggest a change from a closed set of typed actions, and applies it only after:

1. static rules,
2. a planner what-if,
3. a replay of captured production workload on a clone (the digital twin), judged **per tenant**: the target must be
   shown to benefit and every other tenant shown not to be harmed, where "shown" means a whole confidence interval on
   the right side of a threshold,
4. a canary in production under a contract, with automatic rollback.

The question the project set out to answer was what per-tenant gating on a twin adds over judging the workload as a whole
and over a canary alone.

## 2. What exists and is tested

| Part | State |
|---|---|
| Control plane: accounts, organizations, roles, audit, row-level isolation | Built; 172 automated tests with the gate and engine |
| Multi-tenant data plane: partition and role per tenant, row-level security, replica, pooler, least-privilege executor | Built; 36 tests |
| Workload driver (TPC-C transactions and analytical queries, open loop) and evaluation harness | Built; 20 tests |
| Telemetry collector | Built, tested |
| Typed action space and executor with stored inverses | Built, tested |
| Digital twin: delayed standby, clones frozen at a known moment, replay, planner what-if | Built; 47 tests on a miniature data plane |
| Safety gate, verification engine, canary controller | Built, tested |
| Rule-based proposer | Built, tested, finds the demo's missing index on live telemetry |
| LLM agent | Built and tested against a scripted model. **Never run against a live model** (no API key was available) |
| Web console | Built; sign-in, session handling, every page and the change lifecycle covered by browser tests |

Three browser scenarios drive the running stack end to end:

- a change applied under a canary, kept, and rolled back on request;
- the full loop through the twin: proposal, static rules, planner check, eight replay pairs, a verdict, a person's
  decision, canary, apply, rollback;
- a harmful change (dropping an index a tenant depends on) undone by the canary without anyone asking: the tenant's p95
  went to 3.69x its baseline against a contract of 1.15x.

## 3. What went wrong along the way, and what it taught

These are the findings the project did not plan to have. They changed its design more than anything that went right.

**No scenario harmed a neighbour.** The suite's "traps" were meant to be changes that help one tenant and hurt another.
On this stack none did, measured at the class-level p95 the objectives are written in:

- The parallelism scenarios changed nothing: the row-security helper functions are marked parallel-unsafe, so no tenant
  query ever runs in parallel. A "1.3x slowdown" in an early pilot was drift.
- The index built for every tenant did not measurably slow the writers: OLTP latency here is about 85% round trips
  outside the database, and maintaining one more index is lost in it.
- Class-level p95 is set by the slowest kind of operation in the class, so a query that becomes many times faster or
  slower can leave it unchanged.
- Raising total load to make contention visible ran into a lock convoy on each tenant's few warehouses, which collapses
  the run instead of degrading it.

**The laptop is not a measuring instrument.** Under sustained load its speed varied by 5-24%, following CPU frequency,
with no operating-system indicator of a limit; the charger dropped out during several runs and those drops line up to
the second with the anomalies they caused. The same workload profile used 220% to 396% of a core in different sessions.

**The first gate was not trustworthy.** Thirty no-action (A/A) twin runs, where both arms are identical, were judged by
the gate as first built. It returned a false "harm shown" in 2 of 9 verdicts and no correct decisive verdict. The
interval it used came from resampling short time buckets inside each replay; it could not see that two arms run one after
the other differ by themselves (5-12% from pair to pair on this machine, and the tenants drift together).

**The gate was changed, once, on that evidence.** The unit of evidence is now the replay pair. The interval is Student's
t on the logarithm of each pair's ratio; one pair decides nothing; harm must be seen with each arm having run first. A
bootstrap over pairs was considered and rejected because with three pairs its nominal 95% interval covered the truth 75%
of the time on the A/A data. Re-judged on the same thirty runs, the new gate makes no false call (0 of 3,400 reordered
sequences) and no decisive one either.

**Verification bugs found by using the system.**

- The capture recorded the pooler's own `SET application_name` statements as tenant transactions; for a tenant with two
  connection classes half of one class's samples were no-ops.
- The planner what-if judged a tenant's index across every tenant's partition, where it could save only that
  partition's share, and rejected the index the demo is about.
- The canary measures latency inside the database, so harm that is only queueing at the pooler (a one-connection cap)
  is invisible to it. This one is not fixed; it is stated.

## 4. Closing evaluation

Protocol: [tools/final/protocol.md](../tools/final/protocol.md), committed before anything was run.

### Part A. What each gate decides when the size of the harm is known (semi-synthetic)

No change in the action space was found that harms a neighbour, so the decision rule was examined another way. Into the
28 real no-action replay pairs, a slowdown of known size was injected into one neighbouring tenant's class in the
treatment arm, and the target's own class was made faster by the factor the demo index showed (0.10x). The noise is real;
the effect is not. This says what the rule does with an effect of a given size under this machine's noise. It does not
say the twin reproduces production, and it is not the twin catching a regression.

Share of sequences in which the change would proceed to production (APPROVE), and in which the per-tenant gate rejects it
for harm, by replay budget. Target `t_mixed`, whose analytical class is a large share of total database time; the
neighbours' classes pooled; 20 random orderings of the pairs (dependent draws, so these are indicative).

| Slowdown of one neighbour | Per-tenant: rejected for harm (8 / 14 / 28 pairs) | Per-tenant: approved | Aggregate gate: approved (8 / 14 / 28 pairs) |
|---|---|---|---|
| none | 0% / 0% / 0% | 0% | 57% / 65% / 65% |
| +5% (the margin) | 0% / 0% / 0% | 0% | 43% / 66% / 67% |
| +10% | 3% / 4% / 3% | 0% | 39% / 55% / 63% |
| +20% | 31% / 42% / 53% | 0% | 20% / 28% / 43% |
| +50% | 83% / 85% / 92% | 0% | 3% / 5% / 7% |
| +100% | 88% / 94% / 100% | 0% | 1% / 0% / 0% |

What it shows:

- **The per-tenant gate let no injected harm through**, at any size or budget. With eight or more pairs it rejected most
  slowdowns of 50% or more. Slowdowns of 10-20% it mostly could not tell from noise; it held them as inconclusive, which
  blocks the change without identifying the harm.
- **The aggregate gate approved a change that slowed one neighbour by 20% in 20-43% of sequences**, because the target's
  gain outweighs it in total time. This is the effect the project is about, shown under real noise with a synthetic
  effect.
- **That advantage depends on the workload.** With target `t_analytic`, whose class is a small share of total time, the
  aggregate gate approved almost nothing (at most 5%), harmful or not: it is not that it protects neighbours, it simply
  cannot see a benefit that small in the total.
- **Three pairs are not enough**: the per-tenant gate rejected 16% of +50% slowdowns and 36% of +100%.
- **The per-tenant gate approved nothing, including the harmless change.** On this machine its price is that nothing
  passes without a person.

Full tables, per neighbour and with the stalled pair left out: [evidence/final/power.txt](evidence/final/power.txt).

### Part B. Real trials through the loop

Still running when this version of the report was committed. Its results will be added here, next to what the protocol
said to expect, whatever they are.

## 5. What has not been shown

- **That the twin gate approves a safe change.** On this laptop a neighbour's ratio varies by 5-12% between identical
  pairs, so showing it unharmed at a 5% margin would take tens to a hundred pairs. Whether a machine with separate,
  steady cores brings that down has not been measured; a protocol and runner for that measurement exist
  (`tools/aa/`) and were not run for want of a machine.
- **That the twin catches a real harmful change.** None was found to catch.
- **That the twin's prediction matches production in general.** There are a handful of pairs for one kind of action.
- **Anything about the LLM agent's proposals.** It has not been run against a live model.
- **Rates with meaningful confidence intervals**, for anything.

## 6. Related work

A focused search, not a systematic review. It is enough to place the project, not enough to claim novelty.

- **Validating a change before or after applying it.** Azure SQL Database's auto-indexing service recommends indexes for
  every database, implements them automatically for many, and validates them, reverting regressions
  (Das et al., SIGMOD 2019). Oracle's automatic indexing creates an index invisible, checks it against statements, and
  makes it visible only if they improve; Oracle Database Replay captures a production workload and replays it on a test
  system with its timing and concurrency. DBPilot's twin is this idea, not a new one.
- **Safe tuning.** OnlineTune (Zhang et al., SIGMOD 2022) tunes a live cloud database while keeping configurations above
  a safety threshold tied to baseline performance. The threshold is on the instance as a whole.
- **LLM-driven tuning.** GPTuner, λ-Tune (SIGMOD 2025) and AgentTune use language models to choose configurations, and
  bound cost or filter invalid settings. The ones found are evaluated on one workload with aggregate metrics.
- **Multi-tenant objectives.** Tempo (Tan and Babu, VLDB 2016) tunes a resource manager to meet per-tenant objectives
  stated declaratively, in parallel data platforms.
- **Measurement noise.** Run-to-run variability on shared machines is well documented (Maricq et al., OSDI 2018; Uta et
  al., NSDI 2020). Duet benchmarking (Bulej et al., ICPE 2020) runs the two versions being compared at the same time on
  the same machine and compares them pairwise, improving accuracy by factors of 2 to 80. That is the remedy this
  project's A/A result points to: its twin runs the arms one after the other.

In this search no system was found that gates an automatically proposed change on per-tenant non-inferiority in a shared
instance. That is the absence of a result in a short search, not evidence that none exists.

## 7. What a continuation should do first

1. Run the two twin arms at the same time on separate cores, as duet benchmarking does, and repeat the A/A measurement.
   The tenants drifted together between sequential arms, which is the kind of noise concurrent arms cancel.
2. Repeat it on a machine whose cores are not shared and whose speed is steady (`tools/aa/run_linux.sh`).
3. Only if neighbours can then be shown unharmed in a practical number of pairs, run the scenario suite.
4. Find a real cross-tenant harm before claiming anything about catching one. The honest candidates are contention for
   CPU once the row-security helpers are parallel-safe, and write-heavy tenants at the full-size dataset.
5. Give the canary a measure of client-side waiting, or keep concurrency caps away from it.
6. Run the LLM agent against a live model and compare what it proposes with the rules.
