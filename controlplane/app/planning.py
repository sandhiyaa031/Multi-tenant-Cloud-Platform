"""Estimates that help a person read evidence. Nothing here decides anything.

Both answer a question the closing evaluation raised: how much evidence would a
verdict have needed, and how often does the canary's rule fire when nothing changed?
"""
import math
import statistics
from datetime import datetime

from app.engine import rollback_reason, window_breaches
from dbpilot_core.gate import GatePolicy, t_quantile

MAX_PAIRS = 500


def pairs_needed(pair_ratios: list[float], limit: float, comparisons: int, family_alpha: float = 0.05) -> dict | None:
    """How many replay pairs would show this ratio to lie below `limit`, if the pairs kept
    disagreeing as much as the ones measured so far.

    Solves the gate's own condition (upper end of the t interval on ln ratio <= ln limit)
    for the number of pairs, with the error allowance divided among the comparisons and
    among as many looks as there are pairs, as the engine does. It is a planning estimate:
    it assumes the mean and the pair-to-pair spread stay what they are.
    """
    logs = [math.log(r) for r in pair_ratios if r and r > 0]
    if len(logs) < 2:
        return None
    mean, sd = statistics.fmean(logs), statistics.stdev(logs)
    out = {"pairs": len(logs), "sd_log": round(sd, 4), "limit": limit}
    if mean >= math.log(limit):
        return {**out, "reachable": False, "needed": None}
    target = math.log(limit)

    def enough(n: int) -> bool:
        alpha = family_alpha / (n * max(1, comparisons))
        return mean + t_quantile(1 - alpha / 2, n - 1) * sd / math.sqrt(n) <= target

    # The upper end of the interval falls as pairs are added, so look first at a coarse ladder of
    # sizes, then step through the one stretch that holds the answer. Each evaluation of the
    # t quantile costs about a millisecond, and a full scan to MAX_PAIRS for each of a verdict's
    # keys made one page request take seconds.
    ladder = [n for n in (2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128, 160, 192, 256, 320, 400) if n < MAX_PAIRS] + [MAX_PAIRS]
    previous = 1
    for rung in ladder:
        if enough(rung):
            for n in range(previous + 1, rung + 1):
                if enough(n):
                    return {**out, "reachable": True, "needed": n}
        previous = rung
    return {**out, "reachable": True, "needed": None}   # more than MAX_PAIRS


def twin_planning(verdict: dict) -> dict:
    """Per (tenant, class) of a stored per-tenant verdict: pairs needed to show it unharmed."""
    effects = verdict.get("effects") or {}
    if verdict.get("mode") != "per_tenant":
        return {}
    limit = 1 + GatePolicy().max_regression
    measured = [k for k, e in effects.items() if e.get("pair_ratios")]
    out = {}
    for key in measured:
        estimate = pairs_needed(list(effects[key]["pair_ratios"].values()), limit, len(measured))
        if estimate:
            out[key] = estimate
    return out


def canary_false_alarms(windows: list[dict], change_times: list[datetime], *, baseline_windows: int = 3,
                        canary_windows: int = 3, limit: float | None = None, min_txns: int = 5) -> dict:
    """Replays the canary's own rule over telemetry recorded while nothing was being changed.

    `windows`: [{"end": datetime, "keys": {"role/CLASS": (p95 ms, transaction count)}}], oldest first.
    Every run of `baseline_windows` + `canary_windows` consecutive windows that contains no
    moment at which a change was applied or undone is judged as the engine would judge a
    canary with the default contract. Whatever it flags is a false alarm, because nothing
    changed. Runs overlap, so the share is indicative, not a rate with an interval.
    """
    policy = GatePolicy()
    if limit is None:
        limit = round(1 + policy.max_regression + policy.contract_tolerance, 3)
    span = baseline_windows + canary_windows
    gaps = sorted((b["end"] - a["end"]).total_seconds() for a, b in zip(windows, windows[1:]))
    interval = gaps[len(gaps) // 2] if gaps else 0.0
    keys = sorted({k for w in windows for k in w["keys"]})
    per_key = {k: {"judged": 0, "alarms": 0, "counts": []} for k in keys}
    sequences = alarms = 0
    for i in range(0, len(windows) - span + 1):
        run = windows[i:i + span]
        start, end = run[0]["end"], run[-1]["end"]
        if (end - start).total_seconds() > interval * (span - 1) * 1.5:       # telemetry gap inside the run
            continue
        if any(start.timestamp() - interval <= t.timestamp() <= end.timestamp() for t in change_times):
            continue
        before, after = run[:baseline_windows], run[baseline_windows:]
        baseline = {}
        for key in keys:
            values = [w["keys"][key][0] for w in before if key in w["keys"] and w["keys"][key][1] >= min_txns]
            if values:
                baseline[key] = sum(values) / len(values)
        if not baseline:
            continue
        contract = {key: limit for key in baseline}
        history = [{"breaches": window_breaches(baseline, w["keys"], contract, min_txns), "telemetry": True} for w in after]
        sequences += 1
        alarms += rollback_reason(history) is not None
        for key in baseline:
            breached = sum(any(b.startswith(key + ":") for b in h["breaches"]) for h in history)
            per_key[key]["judged"] += 1
            per_key[key]["alarms"] += breached >= 2
            per_key[key]["counts"] += [w["keys"][key][1] for w in after if key in w["keys"]]
    return {
        "limit": limit, "min_txns": min_txns, "window_s": round(interval, 1), "windows": len(windows),
        "sequences": sequences, "alarms": alarms,
        "keys": {k: {"sequences": v["judged"], "alarms": v["alarms"],
                     "median_txns_per_window": statistics.median(v["counts"]) if v["counts"] else None}
                 for k, v in per_key.items() if v["judged"]},
    }
