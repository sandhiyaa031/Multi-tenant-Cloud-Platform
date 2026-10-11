"""Part B of tools/final/protocol.md: tables from the trial records. Everything printed is computed from the file.

usage: python tools/final/trials_report.py results/final/trials.jsonl [power-off windows as "ISO,ISO" ...]
"""
import json
import statistics as st
import sys
from datetime import datetime
from pathlib import Path

rows = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()]
outages = [tuple(datetime.fromisoformat(x) for x in arg.split(",")) for arg in sys.argv[2:]]


def invalid(r: dict) -> str | None:
    if "error" in r and "final_state" not in r:
        return f"harness error: {r['error'][:120]}"
    if r.get("twin_transactions") and (r.get("twin_replay_errors") or 0) > 0.01 * 2 * r["twin_transactions"]:
        return f"replay errors {r['twin_replay_errors']} of {2 * r['twin_transactions']}"
    start = datetime.fromisoformat(r["started"])
    end_s = (r.get("window_times") or {}).get("undone_at") or 0
    for off, on in outages:
        if start.timestamp() <= on.timestamp() and (end_s or start.timestamp() + 3000) >= off.timestamp():
            return f"power source changed during the trial ({off:%H:%M:%S}-{on:%H:%M:%S} UTC)"
    return None


valid, dropped = [], []
for r in rows:
    why = invalid(r)
    (dropped if why else valid).append((r, why))
print(f"trials recorded {len(rows)}, valid {len(valid)}, excluded {len(dropped)}")
for r, why in dropped:
    print(f"  EXCLUDED rep {r.get('rep')} {r.get('scenario')} {r.get('config')}: {why}")
valid = [r for r, _ in valid]


def fmt(x, d=2):
    return "n/a" if x is None else f"{x:.{d}f}"


print("\n== every valid trial ==")
for r in valid:
    target = (r["action"].get("tenant_role") or "") + "/"
    line = f" rep {r['rep']} {r['scenario']:17s} {r['config']:19s} -> {r['final_state']:13s} decision in {r['seconds_to_decision']:6.0f} s"
    if r.get("twin_decision"):
        ratios, intervals = r["twin_ratios"], r["twin_intervals"]
        mine = [k for k in ratios if k.startswith(target)]
        others = [k for k in ratios if not k.startswith(target) and intervals[k][0] is not None]
        widest = max(others, key=lambda k: intervals[k][1] - intervals[k][0]) if others else None
        line += f" | twin {r['twin_decision']} after {r['twin_looks']} pair(s)"
        for k in mine:
            line += f"; target {k} {fmt(ratios[k], 3)} [{fmt(intervals[k][0], 3)}, {fmt(intervals[k][1], 3)}]"
        if widest:
            line += f"; widest neighbour {widest} {fmt(ratios[widest])} [{fmt(intervals[widest][0])}, {fmt(intervals[widest][1])}]"
    if r.get("canary_outcome"):
        line += f" | canary {r['canary_outcome']}"
    print(line)
    if r["final_state"] in ("FAILED",):
        print(f"      reason: {r.get('state_reason')}")

print("\n== production ratio, live / mean(before, after), by scenario (trials that reached production) ==")
for scenario in dict.fromkeys(r["scenario"] for r in valid):
    for config in ("C2_no_verification", "C3_canary_only", "C4_twin_per_tenant"):
        group = [r for r in valid if r["scenario"] == scenario and r["config"] == config and r.get("reached_production")
                 and r.get("ground_truth") == "live / mean(before, after)"]
        if not group:
            continue
        print(f" {scenario} {config}: {len(group)} trial(s)")
        for key in sorted({k for r in group for k in r["client_ratios"]}):
            v = [r["client_ratios"][key] for r in group if key in r["client_ratios"]]
            print(f"    {key:18s} median {st.median(v):.2f}  range {min(v):.2f}-{max(v):.2f}  (n={len(v)})")

print("\n== twin ratio for each key, by scenario (C4 trials) ==")
for scenario in dict.fromkeys(r["scenario"] for r in valid):
    group = [r for r in valid if r["scenario"] == scenario and r.get("twin_ratios")]
    if not group:
        continue
    print(f" {scenario}: {len(group)} trial(s); verdicts {[r['twin_decision'] for r in group]}; pairs {[r['twin_looks'] for r in group]}")
    for key in sorted({k for r in group for k in r["twin_ratios"]}):
        v = [r["twin_ratios"][key] for r in group if r["twin_ratios"].get(key) is not None]
        iv = [r["twin_intervals"][key] for r in group if r["twin_intervals"].get(key, [None])[0] is not None]
        print(f"    {key:18s} ratios {[round(x, 3) for x in v]}  intervals {[[round(a, 2), round(b, 2)] for a, b in iv]}")
