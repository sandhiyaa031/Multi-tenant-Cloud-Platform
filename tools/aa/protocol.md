# A/A validation of the pair-based gate on a controlled Linux host

Written before the run and not changed afterwards. No treatment is applied anywhere: both arms of every replay pair are
identical, nothing is proposed, verified, applied or rolled back.

## Question
Is the pair-to-pair variation of identical arms on this host low enough for the gate to make useful decisions, so that
running a real treatment (the missing-index scenario) is justified?

## Host
A Linux machine with Docker and at least 20 CPUs (the Compose file pins the planes to CPUs 0-19), not shared with other
work during the run, on mains power, fixed CPU count. `host.txt` records kernel, CPU model, CPU count, memory and the
load average before and after. The stack runs with the same sizes and workload as the E1 laptop runs, so only the host
differs: SEED_SCALE 0.3, EVAL_RATE_SCALE 0.5, EVAL_POOL_SIZE 8, TWIN_DELAY_S 60, TWIN_WINDOW_S 55, one replay pair
per twin run.

## Procedure (`tools/aa/run_linux.sh`)
1. Fresh stack: `.env` generated with random secrets, images built from the checked-out commit, four tenants seeded.
2. The harness's base workload (scenario S9_no_change, no overrides) runs for the whole session through the pooler.
3. After 150 s, 16 consecutive A/A twin runs are requested from the twin agent (`action: null`, window 55 s, one
   repetition, the arm that runs first alternating). Each result is stored as `run_NN.json`.
4. The twin agent is called directly; the control-plane API and the engine are not involved.

16 pairs is the smallest number that gives a usable estimate of the standard deviation (15 degrees of freedom) and one
verdict at the largest budget considered practical.

## Validity
A run is invalid if the twin reports FAILED or replay errors exceed 1% of its transactions. Invalid runs are listed and
excluded. If fewer than 12 runs are valid, the session is reported as not interpretable.

## Analysis (`tools/aa/aa_analyze.py`, gate imported unmodified)
- Per tenant and class: the standard deviation of ln(p95 ratio) over the pairs, and of the paired per-request shift.
- Verdicts as the engine would reach them (looks added until decisive) for budgets of 4, 8 and 16 pairs, on consecutive
  groups, for a tenant-scoped target and for an instance-wide one, with the level per look set as the engine sets it.
  REJECT "harm shown" or APPROVE on identical arms is a false verdict; REJECT "no benefit" is the correct one.
- Interval widths at 4, 8 and 16 pairs, and whether every tenant is shown safe at the 5% margin.
- Pairs needed: for each key, the smallest k for which t(k-1) x sd / sqrt(k) <= ln(1.05) at the engine's level for k
  looks and 5 keys, using the observed sd.

## Decision rule (fixed now)
The missing-index test is JUSTIFIED on this host if all of these hold:
1. no false verdict in any group;
2. the pairs needed are at most 12 for every key (a verdict in about 25 minutes of twin time);
3. at 16 pairs every tenant is shown safe.
It is NOT JUSTIFIED if any key needs more than 30 pairs or a false verdict occurs. Anything between is reported as
MARGINAL with the numbers, and the next step is a decision for the project owner (longer windows, concurrent arms, or a
different margin), not a treatment run.
