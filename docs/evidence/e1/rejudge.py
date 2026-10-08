"""Offline re-judgement of the E1 A/A runs with the pair-based gate. No new measurement.

usage: python results/e1/rejudge.py [dir]
The old gate is loaded from git (commit a8c6813) so both judge the same groupings.
"""
import importlib.util
import json
import math
import random
import statistics as st
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))
from dbpilot_core import gate as new  # noqa: E402

src = subprocess.run(["git", "show", "a8c6813:core/dbpilot_core/gate.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
tmp = Path(tempfile.mkdtemp()) / "old_gate.py"
tmp.write_text(src, encoding="utf-8")
spec = importlib.util.spec_from_file_location("old_gate", tmp)
old = importlib.util.module_from_spec(spec)
sys.modules["old_gate"] = old
spec.loader.exec_module(old)

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
SLOS = {("t_analytic", "OLAP"): (95, 2000.0), ("t_bursty", "OLTP"): (99, 150.0),
        ("t_mixed", "OLTP"): (99, 150.0), ("t_steady", "OLTP"): (99, 100.0)}

runs = []
for path in sorted(D.glob("run_*.json")):
    r = json.loads(path.read_text(encoding="utf-8"))
    res = r.get("result")
    if r.get("state") != "DONE" or not res:
        continue
    errors = sum(sum(a["errors"].values()) for a in res["arms"].values())
    if errors > 0.01 * 2 * max(res["transactions"], 1) or r["power_start"].split("|")[0] != r["power_end"].split("|")[0]:
        continue
    runs.append(r)
KEYS = sorted({k for r in runs for a in r["result"]["arms"].values() for k in a["samples"]})
print(f"E1 RE-JUDGEMENT  valid runs {len(runs)}  keys {len(KEYS)}")


def pooled(mod, subset, drop_noops=False):
    p = {"control": {}, "treatment": {}, "wal_c": 0, "wal_t": 0, "order": {}}
    for look, r in enumerate(subset, start=1):
        w = min(15.0, r["result"]["window_s"] * 0.15)
        for arm in ("control", "treatment"):
            samples = {tuple(k.split("/")): [tuple(s) for s in v if not (drop_noops and k.endswith("/OLAP") and s[1] < 10.0)]
                       for k, v in r["result"]["arms"][arm]["samples"].items()}
            mod.pool(p[arm], samples, look, w)
        p["wal_c"] += r["result"]["arms"]["control"]["wal_bytes"]
        p["wal_t"] += r["result"]["arms"]["treatment"]["wal_bytes"]
        p["order"][look] = r["treatment_first"]
    return p


def decide(mod, subset, target, looks, mode="per_tenant", drop_noops=False):
    p = pooled(mod, subset, drop_noops)
    common = dict(mode=mode, cheap_to_undo=True, wal_ratio=p["wal_t"] / p["wal_c"] if p["wal_c"] else None,
                  storage_delta_bytes=0, slo_ms=SLOS)
    if mod is old:
        return old.decide(p["control"], p["treatment"], target, old.GatePolicy(confidence=1 - 0.05 / looks, n_boot=2000), **common)
    return new.decide(p["control"], p["treatment"], target, new.GatePolicy(confidence=1 - 0.05 / looks),
                      treatment_first=p["order"], **common)


def classify(v) -> str:
    if v.decision == "APPROVE":
        return "FALSE BENEFIT"
    if v.decision == "INCONCLUSIVE":
        return "UNDECIDED"
    return "FALSE HARM" if any(x.startswith("harm shown") or "budget" in x for x in v.reasons) else "CORRECT (no benefit)"


def sequence(mod, group, target, mode="per_tenant", drop_noops=False):
    """Engine.verify: add looks until the verdict is not INCONCLUSIVE."""
    for look in range(1, len(group) + 1):
        v = decide(mod, group[:look], target, len(group), mode, drop_noops)
        if v.decision != "INCONCLUSIVE":
            break
    return v, look


def groups(order, size):
    return [order[i:i + size] for i in range(0, len(order) - size + 1, size)]


# ---- 1. old against new, engine-equivalent, consecutive groups
print("\n== 1. engine-equivalent verdicts on consecutive groups (looks added until decisive) ==")
for size in (3, 5, 9, 14, 28):
    if size > len(runs):
        continue
    gs = groups(runs, size)
    for target in ("t_analytic", None):
        line = f" max looks {size:2d} ({len(gs)} groups) target={str(target):10s}"
        for name, mod in (("old", old), ("new", new)):
            if mod is old and size > 9:
                line += "  old: (not run, slow)                        "
                continue
            c = Counter(classify(sequence(mod, g, target)[0]) for g in gs)
            line += f"  {name}: " + ", ".join(f"{k} {n}" for k, n in sorted(c.items()))
        print(line)
    c = Counter(sequence(new, g, None, "aggregate")[0].decision for g in gs)
    print(f"      aggregate gate (new), same groups: " + ", ".join(f"{k} {n}" for k, n in sorted(c.items())))
print(" detail, new gate, max looks 3, target=t_analytic:")
for g in groups(runs, 3):
    v, look = sequence(new, g, "t_analytic")
    print(f"   runs {g[0]['run']:02d}-{g[-1]['run']:02d}: {v.decision} after {look} look(s): {'; '.join(v.reasons)[:200]}")

# ---- 2. the two runs the old gate rejected
print("\n== 2. the disturbed runs (old gate: false 'harm shown') under the new gate ==")
for start in (22, 25):
    g = [r for r in runs if r["run"] >= start][:3]
    for k in (1, 2, 3):
        v = decide(new, g[:k], "t_analytic", 3)
        worst = max(v.effects.items(), key=lambda kv: kv[1].get("ratio") or 0)
        print(f"   runs from {start}, {k} pair(s): {v.decision}; largest ratio {worst[0]} {worst[1]['ratio']:.2f}"
              f" interval {worst[1]['lo']}-{worst[1]['hi']} status {worst[1]['status']}")

# ---- 3. interval widths, new gate
print("\n== 3. new gate: interval width (hi - lo) by number of pairs, alpha as the engine would use with that many looks ==")
print("   pairs groups  " + "  ".join(f"{k.split('/')[0][2:5]}/{k.split('/')[1][:3]}: median [min-max] safe" for k in KEYS) + "   ALL SAFE")
for size in (2, 3, 5, 9, 14, 28):
    if size > len(runs):
        continue
    gs = groups(runs, size)
    vs = [decide(new, g, "t_analytic", size) for g in gs]
    line = f"   {size:5d} {len(gs):6d}  "
    for key in KEYS:
        ws = [v.effects[key]["hi"] - v.effects[key]["lo"] for v in vs if v.effects[key]["hi"] is not None]
        safe = sum(1 for v in vs if v.effects[key]["hi"] is not None and v.effects[key]["hi"] <= 1.05)
        line += (f"{st.median(ws):.2f} [{min(ws):.2f}-{max(ws):.2f}] {safe}/{len(gs)}   " if ws else "n/a   ")
    line += f"{sum(1 for v in vs if all(e['hi'] is not None and e['hi'] <= 1.05 for e in v.effects.values()))}/{len(gs)}"
    print(line)
print(" per-pair ln(p95 ratio) across the 28 runs: sd, and sd without the stalled run 22:")
for key in KEYS:
    v = decide(new, runs, "t_analytic", len(runs)).effects[key]
    logs = {int(r): math.log(x) for r, x in v["pair_ratios"].items()}
    clean = [x for i, x in logs.items() if runs[i - 1]["run"] != 22]
    print(f"   {key:18s} pairs {len(logs):2d}  sd {st.stdev(logs.values()):.3f}  without run 22: {st.stdev(clean):.3f}"
          f"   pooled 28: ratio {v['ratio']:.3f} interval {v['lo']:.3f}-{v['hi']:.3f}")

# ---- 4. many random orderings: how often is A/A rejected or approved?
print("\n== 4. random orderings of the valid runs (dependent draws from the same 28 runs; indicative only) ==")
rng = random.Random(1)
for size in (3, 5, 9):
    c, n = Counter(), 0
    for _ in range(200):
        order = runs[:]
        rng.shuffle(order)
        for g in groups(order, size):
            c[classify(sequence(new, g, "t_analytic")[0])] += 1
            n += 1
    print(f"   max looks {size}: {n} sequences: " + ", ".join(f"{k} {v} ({v / n:.1%})" for k, v in sorted(c.items())))

# ---- 5. sensitivity: without the pooler's no-ops (approximation: OLAP samples under 10 ms dropped)
print("\n== 5. sensitivity: OLAP samples under 10 ms dropped (stands in for the capture filter) ==")
for size in (3, 9, 28):
    gs = groups(runs, size)
    c = Counter(classify(sequence(new, g, "t_analytic", drop_noops=True)[0]) for g in gs)
    vs = [decide(new, g, "t_analytic", size, drop_noops=True) for g in gs]
    ws = {key: [v.effects[key]["hi"] - v.effects[key]["lo"] for v in vs if v.effects[key]["hi"] is not None] for key in KEYS}
    print(f"   max looks {size:2d}: " + ", ".join(f"{k} {n}" for k, n in sorted(c.items())) + "   median widths: "
          + "  ".join(f"{k.split('/')[0][2:5]}/{k.split('/')[1][:3]} {st.median(w):.2f}" if w else f"{k} n/a" for k, w in ws.items()))
n_olap = {k: (st.median(len([s for s in r["result"]["arms"]["control"]["samples"].get(k, []) if s[0] >= 8.25]) for r in runs),
              st.median(len([s for s in r["result"]["arms"]["control"]["samples"].get(k, []) if s[0] >= 8.25 and s[1] >= 10]) for r in runs))
          for k in KEYS if k.endswith("/OLAP")}
print("   median samples per replay after warm-up (all, without no-ops): " + ", ".join(f"{k} {a:.0f} -> {b:.0f}" for k, (a, b) in n_olap.items()))

# ---- 6. why Student's t on the pairs rather than a bootstrap over the pairs
print("\n== 6. A/A coverage of a nominal 95% interval from k pairs: Student's t against a percentile bootstrap over pairs ==")
per_run = {key: [] for key in KEYS}
full = decide(new, runs, "t_analytic", len(runs)).effects
for key in KEYS:
    per_run[key] = [math.log(x) for x in full[key]["pair_ratios"].values()]
nrng = np.random.default_rng(2)
for k in (3, 5, 9):
    hit_t = hit_b = total = 0
    for key in KEYS:
        x = np.array(per_run[key])
        for _ in range(400):
            sub = nrng.choice(x, size=k, replace=False)
            half = new.t_quantile(0.975, k - 1) * sub.std(ddof=1) / math.sqrt(k)
            hit_t += abs(sub.mean()) <= half
            means = nrng.choice(sub, size=(1000, k), replace=True).mean(axis=1)
            lo, hi = np.percentile(means, [2.5, 97.5])
            hit_b += lo <= 0 <= hi
            total += 1
    print(f"   k={k}: interval contains 'no effect' in {hit_t / total:.1%} (t) and {hit_b / total:.1%} (bootstrap over pairs) of {total} subsets")
