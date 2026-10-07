# P0.1d frozen protocol: scale-5 operating point and noise floor (written before any scale-5 trial ran)

DISCLOSURE (researcher degrees of freedom). Scale 5 was chosen AFTER the P0.1c sweep showed it healthy (58 % mean
CPU, 0 drops) while the pre-registered P0.1c band (60-85 %) was met by no scale. The lower bound of the CPU band is
relaxed here from 60 % to 55 % because of that observation. Nothing else is relaxed: the +/-10 % ratio criterion is
unchanged from P0.1 / P0.1c. P0.1c is recorded as failed; this is a new, separate protocol, not a re-reading of it.

Operating point: EVAL_RATE_SCALE=5, EVAL_POOL_SIZE=16 (command-line override; .env untouched), everything else the
laptop profile. Config C2_no_verification, scenario S7_trap_parallelism: for tenant queries this is a NULL action
(RLS helpers are parallel-unsafe, so no tenant plan consults the setting; proven by EXPLAIN and the P0.1b screen).
So these trials measure the noise floor of the production-loop metric at this load. Three trials, one at a time.

Per-trial VALID iff ALL hold (else INVALID, reported, never interpreted as an effect):
  V1 dropped + errored requests <= 1 % of all requests in the run;
  V2 every tenant/class has >= 30 completed requests in each of its before / during / after windows;
  V3 final_state APPLIED and cleanup ROLLED_BACK, and the instance setting is back to 2 afterwards;
  V4 dp-primary median CPU while loaded (external monitor, samples > 2 %) within 55-85 % of 400 % (220-340 %).
Contention PRESENT in a valid trial iff the largest, over tenant/class, of (run-level p95 / its scale-2 reference
run-level p95) is >= 1.5; references from results/cal_run_2.log: t_steady/OLTP 30.6, t_bursty/OLTP 30.1,
t_mixed/OLTP 31.5, t_analytic/OLAP 125.0, t_mixed/OLAP 174.8 ms. Run-level p95 = the harness load_totals p95.

Operating point is FIT for trap testing iff: >= 2 of the 3 trials are VALID, AND in every valid trial contention is
PRESENT, AND every bracketed ratio (harness client_ratios, live / mean(before, after)) of every valid trial lies in
[0.90, 1.10]. Otherwise NOT FIT, with the failing condition named:
  - V1-V4 or contention fail: the operating point is not stable or not contended; load cannot be tuned further here.
  - ratio criterion fails: the production-loop metric cannot resolve 10 % effects even at this load; a switchback
    measurement (alternating on/off within a run) is required before any trap.
Secondary, descriptive only (not used for any decision): per-operation (query-level) p95 ratios from the per-op
capture, bracketed the same way; spread across the three trials.

Not done in this step, by decision: no PARALLEL SAFE change, no gate change, no new scenario, no twin run, no commit.
