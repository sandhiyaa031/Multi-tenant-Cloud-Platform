"""Analysis of an A/A session with the pair-based gate. Implements tools/aa/protocol.md.

usage: python tools/aa/aa_analyze.py <dir>      (needs numpy and dbpilot_core; the api image has both)
"""
import json
import math
import statistics as st
import sys
from collections import Counter
from pathlib import Path

try:
    from dbpilot_core import gate
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))
    from dbpilot_core import gate

D = Path(sys.argv[1])
MARGIN = gate.GatePolicy().max_regression
# The demo organization's objectives (controlplane/demo_seed.py), as the engine would read them.
SLOS = {("t_analytic", "OLAP"): (95, 2000.0), ("t_bursty", "OLTP"): (99, 150.0),
        ("t_mixed", "OLTP"): (99, 150.0), ("t_steady", "OLTP"): (99, 100.0)}

runs, invalid = [], []
for path in sorted(D.glob("run_*.json")):
    r = json.loads(path.read_text(encoding="utf-8"))
    result = r.get("result")
    if r.get("state") != "DONE" or not result:
        invalid.append((r["run"], f"state {r.get('state')}: {r.get('error')}"))
        continue
    errors = sum(sum(a["errors"].values()) for a in result["arms"].values())
    if errors > 0.01 * 2 * max(result["transactions"], 1):
        invalid.append((r["run"], f"replay errors {errors} of {2 * result['transactions']}"))
        continue
    runs.append(r)
print(f"A/A ANALYSIS  dir={D}  runs found {len(runs) + len(invalid)}, valid {len(runs)}")
for number, why in invalid:
    print(f"  INVALID run {number:02d}: {why}")
if len(runs) < 12:
    sys.exit("NOT INTERPRETABLE: fewer than 12 valid runs")
KEYS = sorted({k for r in runs for a in r["result"]["arms"].values() for k in a["samples"]})


def warmup(r) -> float:
    return min(15.0, r["result"]["window_s"] * 0.15)


def decide(subset, target, looks, mode="per_tenant"):
    control, treatment, order, wal_c, wal_t = {}, {}, {}, 0, 0
    for look, r in enumerate(subset, start=1):
        for arm, into in (("control", control), ("treatment", treatment)):
            gate.pool(into, {tuple(k.split("/")): [tuple(s) for s in v] for k, v in r["result"]["arms"][arm]["samples"].items()},
                      look, warmup(r))
        order[look] = r["treatment_first"]
        wal_c += r["result"]["arms"]["control"]["wal_bytes"]
        wal_t += r["result"]["arms"]["treatment"]["wal_bytes"]
    return gate.decide(control, treatment, target, gate.GatePolicy(confidence=1 - 0.05 / looks), mode=mode,
                       wal_ratio=wal_t / wal_c if wal_c else None, slo_ms=SLOS, treatment_first=order)


def classify(v) -> str:
    if v.decision == "APPROVE":
        return "FALSE BENEFIT"
    if v.decision == "INCONCLUSIVE":
        return "UNDECIDED"
    return "FALSE HARM" if any(x.startswith("harm shown") or "budget" in x for x in v.reasons) else "CORRECT (no benefit)"


def sequence(group, target, mode="per_tenant"):
    for look in range(1, len(group) + 1):
        v = decide(group[:look], target, len(group), mode)
        if v.decision != "INCONCLUSIVE":
            break
    return v, look


def short(key: str) -> str:
    return f"{key.split('/')[0][2:5]}/{key.split('/')[1][:3]}"


print("\n== runs ==")
for r in runs:
    res = r["result"]
    errors = sum(sum(a["errors"].values()) for a in res["arms"].values())
    counts = " ".join(f"{short(k)}={sum(1 for s in res['arms']['control']['samples'].get(k, []) if s[0] >= warmup(r))}" for k in KEYS)
    print(f" {r['run']:3d} first={'T' if r['treatment_first'] else 'C'} {r['seconds']:5.0f}s txns {res['transactions']:5d} errors {errors:3d}"
          f"  samples after warm-up (control): {counts}")

# ---- variance
print("\n== pair-to-pair variation of identical arms ==")
full = decide(runs, "t_analytic", len(runs))
sd = {}
for key in KEYS:
    logs = [math.log(x) for x in full.effects[key]["pair_ratios"].values()]
    if len(logs) < 3:
        print(f" {key:18s} too few pairs with enough samples ({len(logs)})")
        continue
    sd[key] = st.stdev(logs)
    print(f" {key:18s} pairs {len(logs):2d}  sd ln(p95 ratio) {sd[key]:.4f}  range {math.exp(min(logs)):.3f}-{math.exp(max(logs)):.3f}"
          f"  mean {math.exp(st.mean(logs)):.3f}")
shifts = []
for r in runs:
    first, second = ("treatment", "control") if r["treatment_first"] else ("control", "treatment")
    values = []
    for key in KEYS:
        seen: dict = {}
        for offset, latency in r["result"]["arms"][first]["samples"].get(key, []):
            if offset >= warmup(r):
                seen.setdefault(offset, []).append(latency)
        for offset, latency in r["result"]["arms"][second]["samples"].get(key, []):
            if offset >= warmup(r) and seen.get(offset):
                values.append(math.log(latency / seen[offset].pop(0)))
    shifts.append(st.median(values))
print(f" paired per-request shift, arm run second / first, per run: sd {st.stdev(shifts):.4f}"
      f"  range {math.exp(min(shifts)):.3f}-{math.exp(max(shifts)):.3f}  mean {math.exp(st.mean(shifts)):.3f}")

# ---- verdicts and widths
print("\n== verdicts as the engine would reach them (consecutive groups) ==")
false_verdicts = 0
for size in (4, 8, 16):
    groups = [runs[i:i + size] for i in range(0, len(runs) - size + 1, size)]
    if not groups:
        continue
    for target in ("t_analytic", None):
        outcomes = [sequence(g, target) for g in groups]
        counts = Counter(classify(v) for v, _ in outcomes)
        false_verdicts += counts["FALSE HARM"] + counts["FALSE BENEFIT"]
        print(f" budget {size:2d} pairs, {len(groups)} group(s), target={str(target):10s}: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items()))
              + "   decided after looks: " + " ".join(str(look) for _, look in outcomes))
        for (v, look), g in zip(outcomes, groups):
            print(f"      runs {g[0]['run']:02d}-{g[-1]['run']:02d}: {v.decision}: {'; '.join(v.reasons)[:260]}")
    aggregate = Counter(sequence(g, None, "aggregate")[0].decision for g in groups)
    print(f"      aggregate gate: " + ", ".join(f"{k} {n}" for k, n in sorted(aggregate.items())))

print(f"\n== interval width (hi - lo); a tenant is shown safe when hi <= {1 + MARGIN:.2f} ==")
safe_at_16 = None
for size in (4, 8, 16):
    groups = [runs[i:i + size] for i in range(0, len(runs) - size + 1, size)]
    if not groups:
        continue
    verdicts = [decide(g, "t_analytic", size) for g in groups]
    line = f" {size:2d} pairs: "
    for key in KEYS:
        widths = [v.effects[key]["hi"] - v.effects[key]["lo"] for v in verdicts if v.effects[key]["hi"] is not None]
        safe = sum(1 for v in verdicts if v.effects[key]["hi"] is not None and v.effects[key]["hi"] <= 1 + MARGIN)
        line += f"{short(key)} {st.median(widths):.3f} safe {safe}/{len(groups)};  " if widths else f"{short(key)} n/a;  "
    all_safe = sum(1 for v in verdicts if all(e["hi"] is not None and e["hi"] <= 1 + MARGIN for e in v.effects.values()))
    print(line + f"ALL SAFE {all_safe}/{len(groups)}")
    if size == 16:
        safe_at_16 = all_safe == len(groups)

# ---- pairs needed
print("\n== pairs needed to show a tenant safe at the margin, from the observed sd ==")
needed = {}
for key, s in sd.items():
    needed[key] = next((k for k in range(2, 201)
                        if gate.t_quantile(1 - (0.05 / k / len(KEYS)) / 2, k - 1) * s / math.sqrt(k) <= math.log(1 + MARGIN)), None)
    print(f" {key:18s} sd {s:.4f} -> {needed[key] if needed[key] else 'more than 200'} pairs")

worst = max((n if n else 10**6) for n in needed.values()) if needed else 10**6
missing = [k for k in KEYS if k not in sd]
if false_verdicts or worst > 30 or missing:
    outcome = "NOT JUSTIFIED"
elif worst <= 12 and safe_at_16:
    outcome = "JUSTIFIED"
else:
    outcome = "MARGINAL"
print(f"\nDECISION (frozen rule): missing-index test {outcome}   false verdicts {false_verdicts}; most pairs needed {worst};"
      f" every tenant safe at 16 pairs: {safe_at_16}; keys without enough pairs: {missing}")
