# P0.1e frozen protocol: operating-point characterization along the analytic-intensity axis
Written and hashed before any run. Approved design: 4 no-action runs, OLTP at scale 0.5, t_analytic OLAP at
10 / 20 / 30 / 40 qps, 7 minutes each (2 min warm-up, 5 min measured), per-operation capture, wait-event sampling.
No code, gate, threshold, baseline, RLS-semantics or workload-implementation change. No action is applied in any run.

## Configuration
- Git: HEAD 965baf3; the only uncommitted change is the audited per-operation-capture edit to
  workload/evaluation/__main__.py (diff hash 294899db7924). This characterization does not use that file.
- Evaluation image dbpilot-evaluation sha256:9633860bc572... (built 2026-10-06T16:25:11Z); stack = the running
  Compose stack, laptop profile from .env (untouched). Instance setting max_parallel_workers_per_gather = 2, RLS
  helper functions parallel-unsafe ('u'), 0 auto.conf overrides (verified before the runs).
- Profiles (results/p01e/profile_L{10,20,30,40}.json): eval.json rates x 0.5 for every OLTP stream and for
  t_mixed OLAP (0.4 qps); t_bursty keeps its 6x burst (15 s of every 60 s); t_analytic OLAP = L qps (all 7 queries,
  uniform). Pool 16 per stream, max_inflight 200, 10 s flush interval (the unchanged workload.driver.run()).
- Runner: results/p01e/run.py (wraps one Recorder instance to timestamp samples; no repository code touched).
  Order of runs: L10, L20, L30, L40, one at a time, 90 s rest and a state snapshot between runs.
- Hashes (first 16 hex of sha256): profile_L10 18949ed3fc817462, L20 f6423316aeca366b, L30 260adc1ab0b671ce,
  L40 fe65f25252fb417f; run.py 666b05f00543bb03; analyze.py cf0d2f26b3c1e3d0; monitor.sh f797362c82b96deb;
  state.sh 510d417ea5fc07a9; runall.sh 4a9d85c014f91f0f.
- Identification: every run writes run_L<level>.json (start/end wall clock, profile hash, container hostname,
  raw samples, error and drop events), ops_L<level>.jsonl, monitor_L<level>.log, state_L<level>_{start,end}.txt,
  console_L<level>.log; progress.log timestamps every step.

## Operationalisation fixed at freeze time (not tuned to any result)
- Measured window: t >= 120 s after generator start (300 s = ten 30-s blocks).
- Judged keys (class-level, all operations pooled): t_steady/OLTP, t_bursty/OLTP, t_mixed/OLTP, t_analytic/OLAP.
  t_mixed/OLAP (0.4 qps, about 12 requests per block) is recorded but NOT judged: too few samples.
- Block statistic: p95 of the block's request latencies (nearest rank, as workload.driver.percentile).
- S2 stationarity per key: ordinary least-squares trend of the ten block p95 values against time. NON-STATIONARY iff the
  trend is statistically significant (two-sided t-test, 95 %, df 8: |t| > 2.306) AND its change over 180 s exceeds 5 %
  of the mean block p95. (A bare "slope <= 5 %" rule would fail by chance on noisy p95 blocks; this is the approved
  rule made testable.) Regime break: any block p95 > 3x the median of that key's block p95 values.
- Wait events: five pg_stat_activity snapshots (0.2 s apart) every ~8 s; share = sum(active backends waiting on
  wait_event_type 'Lock') / sum(active backends) over the measured window.
- CPU: docker stats samples of dp-primary (limit 400 %) with value > 2 %, measured window only.
- Growth: exact row counts of order_line, orders and history for every tenant partition at run start and end.
- New-Order p95: t_steady, operation new_order, all requests in the measured window.

## Criteria (a level QUALIFIES iff all of S1-S6 hold)
- S1 0 errors and 0 dropped requests over the whole run (summary of the unchanged driver).
- S2 stationary on all four judged keys (above).
- S3 dp-primary CPU median 220-320 % (55-80 % of 400) AND 95th percentile of samples <= 380 % (95 %).
- S4 lock-wait share <= 5 %.
- S5 growth <= 5 % for every counted partition.
- S6 collector CPU median <= 80 % (docker stats).
Contention VISIBLE and dose-response smooth (for the selected level L): New-Order p95 at L >= 1.25 x its value at
L10, and every successive step ratio up to L satisfies 0.9 <= p95(next)/p95(previous) <= 2.0.
OPERATING POINT = the highest level that qualifies AND has contention visible AND a smooth dose-response.
If no level does: NO stable contended operating point exists on this laptop with this axis; the result is reported
as such, with the recommendation to change environment, and no further levels or tuning are tried.
If a level qualifies only without visible contention (for example only L10): that is also "no contended point".
Every run is reported, including failures. Invalid or crashed runs are not repeated unless a tool fault (not the
database or the workload) is shown; a repeat would be disclosed.
Analysis: python results/p01e/analyze.py (hashed above; implements exactly this text).
