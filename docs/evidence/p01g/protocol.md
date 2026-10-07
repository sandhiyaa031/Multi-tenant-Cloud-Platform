# P0.1g protocol: full L10-L40 characterization under AC + Best performance (written and hashed before any run)

This is a NEW protocol in a NEW environment state. It does not overwrite, reinterpret or replace P0.1e (results/p01e/), which
was measured while the laptop was (at least at times) on battery in the Balanced power mode. The P0.1e frozen criteria are
applied here UNCHANGED: results/p01g/criteria_p01e_verbatim.md is a byte-identical copy of results/p01e/criteria.md
(hash 0b35db4c46511a1e), and results/p01g/analyze.py is byte-identical to results/p01e/analyze.py (hash cf0d2f26b3c1e3d0),
which implements that text. No criterion, threshold, band, window, test or definition is altered to make any level pass.

## What changed relative to P0.1e (the only differences, all environmental)
- Power: AC connected, Windows power mode "Best performance" (overlay ded574b5-45a0-4f42-8737-46345c09c238 for AC and DC).
- Docker/WSL memory 10 GB (was 7.6 GiB); Docker disk image on D:. CPU count unchanged (24).
- Added measurement only: powerlog.sh (AC / battery / overlay every 20 s) and a capture of PostgreSQL checkpoint and
  autovacuum events per run. Neither feeds any criterion.
## What did NOT change
- CPU pinning (cpuset: dp-primary 0-3, dp-replica 4-5, pgbouncer 6, generator 7-8, twin 12-19 with clone 12-15; control
  plane unpinned) and docker-compose.yml; container memory limits; source code (HEAD 965baf3; only the audited unused
  per-operation-capture edit, diff hash 294899db7924); gate, thresholds, safety criteria; workload and profile definitions.

## Runs
L10, L20, L30, L40 in this order, one at a time; 420 s each (120 s warm-up, 300 s measured); 90 s rest and a database-state
snapshot between runs. Profiles byte-identical to P0.1e (L10 18949ed3fc817462, L20 f6423316aeca366b, L30 260adc1ab0b671ce,
L40 fe65f25252fb417f). Runner logic identical to P0.1e (run.py differs only in /results/p01g paths). Hashes: run.py
e7d69ab03bec24b7, monitor.sh 842754aa27ae1c6e, powerlog.sh 0051bb8500b2b6ac, runall.sh e3b90d1f1efc2677, state.sh
510d417ea5fc07a9. No action, no twin run, no trap.

## Environment at freeze (read-only)
AC Online, battery 49 % charging, overlay AC = DC = Best performance; Docker 24 CPUs, MemTotal 10,426,687,488 B;
.wslconfig memory=10GB processors=24; all containers 0 restarts, no OOM; host memory 3.1 GB free of 15.7 GB.

## Verdict rule (from the unchanged P0.1e text)
A level qualifies iff S1-S6 all hold; the operating point is the highest qualifying level with contention visible
(New-Order p95 >= 1.25x its L10 value) and a smooth dose-response (successive step ratios in [0.9, 2.0]). If none: no stable
contended operating point under the unchanged criteria. No expectation about any level is frozen as a criterion.
Power guard: if AC goes offline or the power overlay changes during a run, that run is reported as power-compromised.
Failures are reported, not repeated, unless a tool fault (not the database or workload) is shown and disclosed.
