# P0.1b frozen protocol: action-sensitivity screen (written before the screen ran)

Why: S7/S10 changed max_parallel_workers_per_gather, which no tenant query consults (the RLS helper functions are
parallel-unsafe, so tenant plans are serial: verified by EXPLAIN as t_analytic). Before another production trial we
measure, per candidate action and per class, whether the action can move tenant latency at all on this stack.

Design: for each condition, 6 rounds; each round has one A block (unchanged) and one B block (action applied), order
alternating AB/BA to cancel drift; the action is really applied and reverted around every block (same SQL as the
executor plan). Tenants connect through PgBouncer as the real roles. Block = 150 OLTP transactions (TPC-C mix, role
t_steady) and 42 OLAP queries (6 x each of the 7 queries, role t_analytic). 15 / 7 warm-up requests are discarded per
block. Statistic per key (t_steady/OLTP, t_analytic/OLAP): p95 of the block's request latencies.
Round ratio = p95(B) / p95(A).

Conditions: AA (no change; noise floor) | S6 index on order_line, all tenants | S10 per_gather=0 (RLS functions as
built) | S10p per_gather=0 with the RLS helper functions marked PARALLEL SAFE (data-plane variant; the A arm is also
SAFE) | work_mem 1MB instance | random_page_cost 1.0 instance | index on stock(s_quantity) all tenants.

Noise floor N(key) = max round ratio of that key in AA (at least 1.10).
CANDIDATE (action, key): all 6 round ratios > N(key). Anything else is NOT a candidate (no near-miss reading).
The screen is VALID only if every AA round ratio is within [0.80, 1.25] for both keys and no block had errors > 2%.
Outcomes:
  >= 1 CANDIDATE: a mechanism exists on this stack (production confirmation is a separate, later step).
  none: no tested action has a latency effect exceeding the screen's noise; P0.1 cannot be completed with this
        action set at this operating point, and the report states the environment change that is required.
Everything else (per-query medians, plan probes) is exploratory and is labelled as such.
Restore check at the end: instance settings back to defaults, test indexes dropped, RLS functions back to 'u'.
