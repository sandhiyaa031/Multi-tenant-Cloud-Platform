# Closing evaluation on the development laptop

Written before any of it was run and not changed afterwards. It is what can be done honestly without a controlled
machine: everything here is measured on one laptop whose speed varies by 5-24% under load and on which identical twin
arms differ by 5-12% from pair to pair (docs/evidence). Numbers from it describe this set-up. They are not claims about
how DBPilot performs in general, and the write-up must say so wherever one is quoted.

The gate, the twin, the engine and the harness are used as they are at the commit recorded in the outputs.

## Part A. What the gate does with a known effect (semi-synthetic, no new measurement)

Question: if a change slowed one tenant by a known amount, what would each gate decide at a given replay budget, under
the noise actually measured between identical arms on this machine?

- Data: the 28 valid no-action (A/A) replay pairs of E1 (`docs/evidence/e1/run_*.json`). Both arms are identical, so the
  only real difference between them is noise.
- Injection: every latency of one (tenant, class) in the treatment arm is multiplied by a constant. That creates an effect
  of exactly known size on top of the real noise. It is a uniform slowdown; real harm can have other shapes (only the
  tail, only during bursts), which this does not cover.
- Two settings, each with the target's own OLAP class made faster by the factor the demo index showed (0.10x):
  target `t_analytic` (small share of total database time) and target `t_mixed` (large share).
- For each setting: no harm; and harm to one neighbouring class at a time (every (tenant, class) not belonging to the
  target), slowed by 5%, 10%, 20%, 50% and 100%. 5% is the gate's margin: it is not harm the gate is meant to refuse.
- Replay budgets: 3, 8, 14 and 28 pairs. Verdicts are reached as the engine reaches them (pairs added one at a time until
  the verdict is not INCONCLUSIVE, the level per look set for that budget, the cluster's objectives included).
- Sequences: 20 random orderings of the 28 pairs, each cut into consecutive groups of the budget's size. These are
  dependent draws from the same 28 pairs; rates from them are indicative, and the stalled pair (run 22) is included
  because it happened. A second table leaves it out.
- Reported per cell, for the per-tenant gate and for the aggregate gate on the same sequences: the share of sequences
  ending APPROVE (the change would proceed to production), REJECT for harm, REJECT for no benefit, and INCONCLUSIVE.
- Reading: a gate "lets harm through" when it APPROVEs a cell with 10% harm or more; it "detects" when it REJECTs for
  harm; INCONCLUSIVE blocks the change without detecting anything.

This is labelled semi-synthetic everywhere it appears. It says what the decision rule does with an effect of a given
size under this machine's noise. It does not say the twin reproduces production effects, and it is never to be described
as the twin catching a regression.

## Part B. Real changes through the real loop (few trials, uncontrolled machine)

Question: what do the loop's stages actually do with a real change that helps a tenant and with a change that does
nothing, and does the twin's ratio for the target agree with production's?

- Scenarios (unchanged from `workload/evaluation/scenarios.json`): S1_missing_index (index for the analytical tenant
  under item lookups) and S9_no_change (a statistics refresh).
- Configurations: C2_no_verification (apply and observe: the ground truth), C3_canary_only, C4_twin_per_tenant with a
  budget of 8 replay pairs. 3 repetitions of each of the 6 cells, repetition by repetition, with the harness as it is.
- Workload: the harness's own, at the laptop profile (rates x0.5, dataset scale 0.3). The demo workload is stopped.
- Validity: a trial is excluded if the harness records an error, the twin's replay errors exceed 1%, or the power source
  changed during it (System log, Kernel-Power 105). Excluded trials are listed.
- Reported: final state and twin verdict of every trial; the twin's ratio and interval for the target and the widest
  neighbour interval; production's bracketed ratio (live / mean(before, after)) per tenant and class, as a median and
  range over repetitions; time to decision.
- Not reported as findings: whether a tenant was "harmed" in a single trial. One trial's production ratio varies by about
  10% on this machine, and the harness's 1.10 threshold was set before that was known.
- Expectations stated in advance, so that a surprise is visible: S1 under C2 shows the target several times faster and no
  neighbour consistently slower; S1 under C4 ends INCONCLUSIVE with the target's benefit shown; S9 under C4 ends
  INCONCLUSIVE or REJECT for no benefit, never APPROVE and never REJECT for harm.

## What this evaluation cannot show

- That the twin gate approves a safe change: on this machine it cannot show neighbours unharmed at a 5% margin.
- That the twin catches a harmful change: no change in the action space has been found that harms a neighbour here.
- Rates with useful confidence intervals: there are too few trials and the machine is not controlled.
