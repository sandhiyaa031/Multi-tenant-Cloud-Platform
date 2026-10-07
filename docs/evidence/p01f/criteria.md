# P0.1f frozen protocol: short A/A diagnostic under AC power (written and hashed before any run)

Purpose: find out whether the unexplained L10 end-of-run stall and the L30 upward creep seen in P0.1e persist now that the
laptop is on AC power with Windows power mode "Best performance". This is a DIAGNOSTIC A/A (no action), NOT a new
P0.1 experiment and NOT a re-run of the P0.1e verdict. Nothing else is changed.

## Environment at freeze (read-only verification before this file was written)
- Power: AC Online (battery charging, about 22 %), power-mode overlay AC = DC = ded574b5-... (Best performance), scheme Balanced.
- Docker 29.6.1: 24 CPUs, MemTotal 10,426,687,488 B; WSL .wslconfig memory=10GB processors=24; swap 3 GiB (automatic).
- docker-compose.yml unmodified; cpuset unchanged: dp-primary 0-3, dp-replica 4-5, pgbouncer 6, generator 7-8, twin 12-19
  (clone 12-15); control-db, api, engine, collector, web unpinned. All containers 0 restarts, no OOM.
- Git: HEAD 965baf3; only uncommitted change = audited per-operation-capture edit (diff hash 294899db7924), unused here.
- Disk image on D: (D:\DockerDesktop\DockerDesktopWSL).

## What is run
Two runs, one at a time, in this order: L10, then L30 (the levels where the stall and the creep appeared); 420 s each
(120 s warm-up, 300 s measured); 90 s rest and a state snapshot between runs. Profiles are byte-identical to P0.1e
(profile_L10 18949ed3fc817462, profile_L30 260adc1ab0b671ce). Same runner logic as P0.1e (run.py differs only in the
/results/p01e -> /results/p01f output paths), same unmodified analyze.py (cf0d2f26b3c1e3d0), same monitor (paths only) and
state script. Added: powerlog.sh (AC / battery / power-mode every 20 s) and a capture of PostgreSQL checkpoint and
autovacuum/analyze events of each run (log files are pruned after 15 min). Hashes: run.py b181197bd34db9a3,
monitor.sh 3b651e8ecd8af7ad, powerlog.sh 03be13d6b686d77c, runall.sh 3f7a2236137b635c, state.sh 510d417ea5fc07a9.
No action is applied; no gate, threshold, criterion, cpuset or code change; no twin run.

## Diagnostic questions, defined now
A. STALL occurred iff, in the measured window (t >= 120 s), EITHER a judged key has a 30-s block p95 > 3x that key's median
   block p95 (the analyzer's regime-break rule) OR any 10-s bucket contains >= 25 requests slower than 1 s.
   (P0.1e L10: block p95 ratios 21x/41x/19x, and 96 requests > 1 s in the last 20 s, unattributed.)
B. CREEP occurred iff the analyzer's trend rule fires on any judged key: statistically significant OLS trend of the ten 30-s block
   p95 values (|t| > 2.306) AND |change over 180 s| > 5 % of the mean. (P0.1e L30: t_steady/OLTP +13.0 %/180 s, t = +4.3.)
C. CPU and latency behaviour: dp-primary CPU median and 95th percentile, pgbouncer, generator, collector CPU, lock-wait share,
   block p95 by key, New-Order p95, errors and drops, partition growth: reported as measured and compared descriptively with
   the P0.1e values (L10: CPU 104 %/150 %, New-Order p95 40.0 ms; L30: 255 %/330 %, 66.9 ms). No pass/fail verdict is derived
   from the comparison.
D. The 4-vCPU primary is "still clearly insufficient" iff at L30 the dp-primary CPU median > 320 % or its 95th percentile > 380 %
   (the P0.1e S3 limits). L40 is not run here, so insufficiency at L40 cannot be re-tested; this will be stated.
E, F. Judgement from A-D, written after the results, with the evidence cited. The analyzer's S1-S6 verdict lines are printed
   by the unchanged script but are NOT a P0.1e verdict.
Power guard: if powerlog shows AC offline or the overlay changed at any point of a run, that run is reported as power-compromised.
Failures and crashes are reported, not repeated, unless a tool fault (not the database or workload) is shown and disclosed.
