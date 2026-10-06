# P0.1c frozen protocol: operating point and noise floor (written before the 5/6/7 sweep and the S7 trials ran)

Question: can this laptop run the data plane at a utilisation where tenants actually contend, without the pooler,
the generator or overload being the cause of what we measure, and is the production-loop metric resolvable there?

Already measured (results/cal_*): scales 0.5, 2, 4, 8 of profiles/eval.json, 90 s each, no action. dp-primary median
CPU 35 / 103 / 199 / 373 % of its 400 %; PgBouncer <= 34 % of its core; generator <= 88 % of its 2 cores.
Scale 8: 3,422 requests dropped, p95 > 1 s (overload). Scale 4: no drops, p95 about 39 ms (no contention yet).

Step 1 sweep: scales 5, 6, 7, same procedure. Operating point = the largest scale s in {4,5,6,7} satisfying ALL of:
  - dp-primary median CPU while loaded within 60-85 % of 400 % (240-340 %);
  - 0 errors and 0 dropped requests in the run;
  - every tenant/class run-level p95 <= 2 x its scale-2 value (not in overload);
  - PgBouncer median CPU <= 50 % of its core and generator median <= 75 % of its two cores
    (otherwise the measuring apparatus, not the database, is the limit).
If no scale qualifies: Path D (more load on this laptop) is INFEASIBLE and is reported as such. No further scales.

Step 2 (only if Step 1 yields an operating point): two S7_trap_parallelism trials, config C2_no_verification, at that
scale (EVAL_RATE_SCALE=s, EVAL_POOL_SIZE=16), one at a time. S7 is a null action for tenant queries (RLS helpers are
parallel-unsafe; proven), so these are noise-floor measurements. Per-query capture is added to the harness output
(secondary instrumentation; not used for any decision). The operating point is FIT for trap testing iff both trials
are valid (drops+errors <= 1 %, every tenant/class >= 30 requests in each window, setting restored) AND every
bracketed ratio in both trials lies within [0.90, 1.10]. Otherwise: NOT FIT; the production-loop metric cannot resolve
10 % effects even at this operating point, and a different measurement design (switchback) is required before any trap.

Not done in this step, by decision: no PARALLEL SAFE change, no gate change, no new scenario, no twin run.
