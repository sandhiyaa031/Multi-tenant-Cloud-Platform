# E1: A/A diagnostic of the existing twin gate (written and hashed before the run; not changed afterwards)

Purpose: a diagnostic of gate mechanics, not an evaluation result and not the start of the evaluation matrix. The gate, the
twin, the engine and the workload driver are used unmodified. Nothing is proposed, verified, applied or rolled back.

## Questions
Q1 What verdicts does the current gate give when both arms are identical (A/A)?
Q2 How does interval width change with replay duration and with the number of pooled replays?
Q3 Can the current gate show every tenant SAFE (the precondition of any APPROVE) in a no-change case? (On A/A an APPROVE
   itself would be a false verdict: the correct decisive outcome with a target tenant is REJECT "no benefit".)
Q4 Does running the two arms one after the other add variation that the gate's interval does not account for?
Q5 What replay budget would the current gate need?

## Design
- Workload: the harness's own base workload, `evaluation.profile_for` on scenario S9_no_change (no overrides), at the
  stack's configured EVAL_RATE_SCALE and EVAL_POOL_SIZE, through the pooler as the tenant roles (results/e1/load.py, a thin
  wrapper around the unchanged workload.driver.run). It runs for the whole session.
- After 150 s of warm-up (the harness default), N = 30 consecutive twin runs are requested directly from the twin node
  agent: POST /runs {"action": null, "window_s": 55, "repetitions": 1, "treatment_first": i odd}. `action: null` is the
  agent's built-in A/A mode. Window, repetitions and the alternation of which arm runs first are what the engine uses on
  this stack (TWIN_WINDOW_S=55, TWIN_REPETITIONS=1, treatment_first = look even). The arms keep the names "control" and
  "treatment"; neither has an action.
- Each run's full result (per-request samples of both arms) is stored as results/e1/run_NN.json.
- Isolation: the control-plane API and engine are not called; no cp.proposals / twin_runs / canaries rows are written; the
  only writes are the tenants' ordinary workload on the data plane (as in every trial), the collector's ordinary telemetry,
  the twin's own scratch clones, and files under results/e1/. Clean-up is: stop the E1 load container. Nothing else is
  stopped, removed or reset. Pre-check: no proposal in PROPOSED/VERIFYING/APPROVED/CANARY on a reachable cluster; twin idle.
- Power state (AC line, overlay) is recorded at the start and end of each run and from the System log (Kernel-Power 105)
  afterwards. The machine is not tuned.

## Validity of a run (reported, never silently dropped)
A run is INVALID if the twin reports FAILED, replay errors exceed 1% of its transactions, or the power source changed
during it. Invalid runs are listed and excluded; the remaining runs keep their order for the groupings below.

## Analysis (results/e1/analyze.py; gate code imported unmodified from core/dbpilot_core/gate.py)
Engine-equivalent policy: GatePolicy(confidence = 1 - 0.05/3, n_boot = 4000), samples pooled with gate.pool(block = look,
warmup = min(15, 0.15 x window_s)), SLOs as stored for the cluster (t_analytic/OLAP p95 2000 ms, t_bursty/OLTP p99 150,
t_mixed/OLTP p99 150, t_steady/OLTP p99 100), wal_ratio and storage delta from the arms, mode per_tenant; the aggregate
mode is reported alongside. Two target settings: target = t_analytic (a tenant-scoped action) and target = None (instance-wide).

A1 Single look: gate.decide on each run alone. Decision counts and per-key statuses.
A2 Engine-equivalent verdicts: consecutive triplets of valid runs (1-3, 4-6, ...). Looks are added one at a time and
   judged on the pooled samples, stopping at the first decision that is not INCONCLUSIVE, exactly as Engine.verify does.
   Classification: REJECT with "harm shown" = FALSE HARM; APPROVE = FALSE BENEFIT; REJECT "no benefit" = CORRECT;
   INCONCLUSIVE = UNDECIDED.
A3 Width against budget: interval width (hi - lo) per key, at the engine-equivalent alpha, for the first k valid runs
   pooled, k = 1, 2, 3, 6, 12, 24, all; and for disjoint groups of size 1, 3 and 6 (median and range of widths). Duration:
   each run truncated to its first 20 s, 35 s and full window after warm-up, at k = 1 and k = 3. Also whether every key
   is SAFE (hi <= 1.05) at each budget.
A4 Between-arm variation:
   (a) coverage: share of single-run intervals that contain 1.0, at the engine alpha and at a plain 95% interval;
   (b) calibration: for each key, the standard deviation across runs of ln(ratio), divided by the median bootstrap
       standard error ((ln hi - ln lo) / 3.92 from the 95% interval). A value near 1 means the interval accounts for the
       run-to-run variation; clearly above 1 means variation between arms that the bootstrap does not see;
   (c) common mode: correlation across runs between the keys' ln(ratio) (arms that differ by a machine-wide speed shift
       move all keys together), and the same on the median instead of p95;
   (d) order: mean of ln(arm executed second / arm executed first) per key, with its t statistic;
   (e) paired shift (descriptive): both arms replay the same transactions at the same offsets, so each request can be
       paired with itself; per run, the median of ln(latency in arm run second / latency in arm run first) over all
       paired requests and per key. Its spread across runs is the size of the arm-to-arm speed difference;
   Reading rule fixed now: the between-arm problem is "visible" if calibration (b) exceeds 1.5 for a majority of keys, or
   95% coverage (a) is below 85%, or the order effect (d) has |t| > 3 for any key; "not visible" if (b) <= 1.25 for all
   keys and coverage >= 90%; otherwise "unclear".
   A key with fewer than the gate's 30 samples per arm in one run cannot be measured at one look; for such a key (b)-(d)
   are computed on triplets instead and this is stated.
A5 Budget: fit width = a x k^-b per key from A3 (median width of disjoint groups of 1, 3, 6, 10, 15 and all runs) and report the k at which the widest key's half-width
   would reach 0.05 (the margin), with the caveat that this extrapolates.

## Known before the run (from the two-run tool check in results/e1/toolcheck/, which is not part of E1)
- The tool check ran on battery (the adapter dropped at 16:33:25Z while the machine was idle). E1 itself starts only with
  the AC line Online, because a 70-minute load would drain the battery; no other power requirement is imposed.
- The capture contains the pooler's own `SET application_name='...'` statements. They appear when a tenant uses two
  connection classes (t_mixed), are parsed as transactions of that class and are replayed, so about half of the
  t_mixed/OLAP samples (and about 8% of t_mixed/OLTP) are 2-5 ms no-ops. This is how the gate has always been fed; the
  frozen analysis keeps the samples exactly as the gate receives them. A sensitivity analysis that drops OLAP samples
  under 10 ms is exploratory and reported separately.

No parameter is changed after the run starts. A tool fault is disclosed; data lost to a tool fault may be re-collected.
