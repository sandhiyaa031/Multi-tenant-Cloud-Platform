# P0.1 evidence: looking for a production trap that harms a tenant

Raw outputs, kept as negative evidence. Pilot observations, not results; nothing here supports a claim about how well DBPilot works.
Each protocol file was written before the run it governs.

| Files | What |
|---|---|
| `p01_criteria.md`, `p01.jsonl`, `p01_trial_*.log`, `p01_monitor_*.log`, `p01_progress.log` | 5 production trials (2 x S7 control, 3 x S10, no verification). Frozen FAILURE criterion met: no measurable harm; the production-loop metric had a ratio spread of 0.78-1.06 on a null action |
| `p01b_criteria.md`, `p01b_screen.jsonl`, `p01b_screen.log` | Action-sensitivity screen (7 conditions, 6 counterbalanced rounds). No candidate at class-p95 level. RLS helper functions are parallel-unsafe, so S7/S10 never touched tenant queries; with them parallel-safe, several queries change about 2x but class p95 does not |
| `p01c_criteria.md`, `cal_*.json`, `cal_run_*.log`, `cal_stats.log`, `cal2_*` | Capacity sweep at scales 0.5-8. The database saturates before the pooler or generator; no scale met the pre-registered operating band |

Reproduce the screen: `docker compose run --rm -e DP_OWNER_URL=... --entrypoint python evaluation -m evaluation.screen`.
