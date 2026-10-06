# P0.1 frozen protocol (written before any trial ran)

Trials (one at a time, config C2_no_verification, laptop profile from .env, no tuning between trials):
order  1 S7_trap_parallelism (control)   2 S10_trap_parallelism_off   3 S10_trap_parallelism_off
       4 S7_trap_parallelism (control)   5 S10_trap_parallelism_off

Metric (existing harness, unchanged): per tenant/class, median of per-10s-interval p95 while the change was live,
divided by the mean of the same statistic before and after ("bracketed ratio"). Also after/before ("drift").

INVALID trial (excluded, never read as harm): requests dropped+errored > 1% of all requests in the run;
or any tenant/class with < 30 completed requests in any of its before/during/after windows;
or the instance setting not restored to 2 after the trial; or the trial errored / no 'after' window.

SUCCESS (all must hold):
 1. t_analytic/OLAP bracketed ratio > 1.10 in at least 2 of the 3 valid S10 trials, with the S10 median > 1.10
    and above the worst S7 control ratio for that key.
 2. drift (after/before) within 0.95-1.05 for every key in the S10 trials.
 3. S7 control bracketed ratios within 0.90-1.10 for every key.
 4. no trial of the 5 hit the overload guard; the setting was restored every time.
FAILURE: t_analytic/OLAP S10 ratio < 1.10 (median of valid S10 trials, or fewer than 2 of 3 above 1.10).
If neither SUCCESS nor FAILURE holds (e.g. invalid trials), the result is INCONCLUSIVE and is reported as such.
Allowed adjustment on FAILURE: only a rate increase of the analytic stream; NOT applied without a new instruction.

Mechanism evidence (external monitor, not part of the harness): SHOW max_parallel_workers_per_gather every 15 s,
Workers Launched of an EXPLAIN ANALYZE scan of ch.order_line_t_analytic every 30 s, dp-primary CPU% every 15 s.
