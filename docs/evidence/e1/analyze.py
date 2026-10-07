"""E1 analysis. Implements results/e1/protocol.md. The gate is imported unmodified from core/dbpilot_core/gate.py.

usage: python results/e1/analyze.py [dir]
"""
import json
import math
import statistics as st
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))
from dbpilot_core import gate  # noqa: E402

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
LOOKS = 3
POLICY = gate.GatePolicy(confidence=1 - (1 - gate.GatePolicy().confidence) / LOOKS, n_boot=4000)
SLOS = {("t_analytic", "OLAP"): (95, 2000.0), ("t_bursty", "OLTP"): (99, 150.0),
        ("t_mixed", "OLTP"): (99, 150.0), ("t_steady", "OLTP"): (99, 100.0)}

# ---- load and validate
runs, invalid = [], []
for path in sorted(D.glob("run_*.json")):
    r = json.loads(path.read_text(encoding="utf-8"))
    res = r.get("result")
    why = None
    if r.get("state") != "DONE" or not res:
        why = f"state {r.get('state')}: {r.get('error')}"
    else:
        errors = sum(sum(a["errors"].values()) for a in res["arms"].values())
        if errors > 0.01 * 2 * max(res["transactions"], 1):
            why = f"replay errors {errors} of {2 * res['transactions']}"
        elif r["power_start"].split("|")[0] != r["power_end"].split("|")[0]:
            why = f"power source changed: {r['power_start']} -> {r['power_end']}"
    (invalid if why else runs).append((r, why))
print(f"E1 A/A ANALYSIS  dir={D}")
print(f"runs found {len(runs) + len(invalid)}, valid {len(runs)}, invalid {len(invalid)}")
for r, why in invalid:
    print(f"  INVALID run {r['run']:02d}: {why}")
runs = [r for r, _ in runs]
if not runs:
    sys.exit("no valid runs")
KEYS = sorted({k for r in runs for a in r["result"]["arms"].values() for k in a["samples"]})


def warmup(r) -> float:
    return min(15.0, r["result"]["window_s"] * 0.15)


def pooled(subset, trunc=None):
    """Pools runs exactly as Engine.verify does; `trunc` keeps only the first `trunc` seconds after warm-up."""
    p = {"control": {}, "treatment": {}, "wal_c": 0, "wal_t": 0, "storage": 0}
    for look, r in enumerate(subset, start=1):
        w = warmup(r)
        for arm in ("control", "treatment"):
            samples = {tuple(k.split("/")): [tuple(s) for s in v if trunc is None or s[0] < w + trunc]
                       for k, v in r["result"]["arms"][arm]["samples"].items()}
            gate.pool(p[arm], samples, look, w)
        p["wal_c"] += r["result"]["arms"]["control"]["wal_bytes"]
        p["wal_t"] += r["result"]["arms"]["treatment"]["wal_bytes"]
        p["storage"] = r["result"]["arms"]["treatment"].get("storage_delta_bytes", 0)
    return p


def decide(p, target, mode="per_tenant"):
    return gate.decide(p["control"], p["treatment"], target, POLICY, mode=mode, cheap_to_undo=True,
                       wal_ratio=p["wal_t"] / p["wal_c"] if p["wal_c"] else None,
                       storage_delta_bytes=p["storage"], slo_ms=SLOS)


def classify(v) -> str:
    if v.decision == "APPROVE":
        return "FALSE BENEFIT (APPROVE)"
    if v.decision == "INCONCLUSIVE":
        return "UNDECIDED (INCONCLUSIVE)"
    return "FALSE HARM (REJECT)" if any(x.startswith("harm shown") or "budget" in x for x in v.reasons) else "CORRECT (REJECT: no benefit)"


def fmt(x, d=2):
    return "  n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


# ---- run table
print("\n== runs ==")
print("run first  secs  txns  window errors  wal_T/C   n per key (control)")
for r in runs:
    res = r["result"]
    errors = sum(sum(a["errors"].values()) for a in res["arms"].values())
    n = " ".join(f"{k.split('/')[0][2:5]}/{k.split('/')[1][:3]}={len(res['arms']['control']['samples'].get(k, []))}" for k in KEYS)
    print(f"{r['run']:3d} {'T' if r['treatment_first'] else 'C'}    {r['seconds']:5.0f} {res['transactions']:5d} {res['window_s']:6.1f} {errors:5d}"
          f"  {res['arms']['treatment']['wal_bytes'] / max(res['arms']['control']['wal_bytes'], 1):6.3f}   {n}")

# ---- A1 single look
print("\n== A1 single look (one 55 s replay per arm), engine-equivalent policy ==")
single = []
for target in ("t_analytic", None):
    counts, status = Counter(), {k: Counter() for k in KEYS}
    for r in runs:
        v = decide(pooled([r]), target)
        counts[classify(v)] += 1
        for k in KEYS:
            status[k][v.effects.get(k, {}).get("status", "NO_DATA")] += 1
        if target == "t_analytic":
            single.append(v)
            if v.decision != "INCONCLUSIVE":
                print(f"   run {r['run']:02d}: {v.decision} {v.reasons}")
    print(f" target={target}: " + ", ".join(f"{k} {n}" for k, n in counts.most_common()))
    for k in KEYS:
        print(f"    {k:18s} " + ", ".join(f"{s} {n}" for s, n in status[k].most_common()))
agg = Counter(decide(pooled([r]), None, "aggregate").decision for r in runs)
print(" aggregate gate: " + ", ".join(f"{k} {n}" for k, n in agg.most_common()))
print(" per-run p95 ratio (treatment/control) and interval at engine alpha, target=t_analytic:")
for r, v in zip(runs, single):
    print(f"   run {r['run']:02d} " + "  ".join(
        f"{k.split('/')[0][2:5]}/{k.split('/')[1][:3]} {fmt(v.effects[k]['ratio'])} [{fmt(v.effects[k]['lo'])},{fmt(v.effects[k]['hi'])}]"
        for k in KEYS if k in v.effects))

# ---- A2 engine-equivalent sequences
print("\n== A2 engine-equivalent verdicts (triplets, looks added until decisive, max 3) ==")
triplets = [runs[i:i + LOOKS] for i in range(0, len(runs) - LOOKS + 1, LOOKS)]
for target in ("t_analytic", None):
    counts = Counter()
    print(f" target={target}")
    for t in triplets:
        for look in range(1, LOOKS + 1):
            v = decide(pooled(t[:look]), target)
            if v.decision != "INCONCLUSIVE":
                break
        counts[classify(v)] += 1
        print(f"   runs {t[0]['run']:02d}-{t[-1]['run']:02d}: {v.decision} after {look} look(s): {'; '.join(v.reasons)[:230]}")
    print("   SUMMARY: " + ", ".join(f"{k} {n}" for k, n in counts.most_common()))
    agg = Counter()
    for t in triplets:
        agg[decide(pooled(t), None, "aggregate").decision] += 1
    print("   aggregate gate on the same triplets (3 looks pooled): " + ", ".join(f"{k} {n}" for k, n in agg.most_common()))

# ---- A3 width against budget
ALPHA = (1 - POLICY.confidence) / len(KEYS)
MARGIN = POLICY.max_regression


def effects(subset, alpha=ALPHA, trunc=None, metric="p95"):
    p = pooled(subset, trunc)
    pol = gate.GatePolicy(**{**POLICY.__dict__, "metric": metric})
    rng = np.random.default_rng(0)
    return {k: gate.compare(p["control"].get(tuple(k.split("/")), []), p["treatment"].get(tuple(k.split("/")), []), pol, alpha, rng)
            for k in KEYS}


def width(e):
    return None if e.lo is None else e.hi - e.lo


print(f"\n== A3 interval width (hi - lo) against budget; alpha per key {ALPHA:.5f}; SAFE needs hi <= {1 + MARGIN:.2f} ==")
print(" cumulative (first k valid runs pooled):")
print("    k  " + "  ".join(f"{k:>17s}" for k in KEYS) + "   all SAFE")
for k in [x for x in (1, 2, 3, 6, 12, 24) if x < len(runs)] + [len(runs)]:
    e = effects(runs[:k])
    safe = all(e[key].hi is not None and e[key].hi <= 1 + MARGIN for key in KEYS)
    print(f"   {k:2d}  " + "  ".join(f"{fmt(width(e[key])):>5s} [{fmt(e[key].lo)},{fmt(e[key].hi)}]" for key in KEYS) + f"   {safe}")
print(" disjoint groups (median width, [min-max], share of groups in which the key is SAFE):")
group_medians: dict[str, dict[int, float]] = {k: {} for k in KEYS}
for size in [s for s in (1, 3, 6, 10, 15) if s <= len(runs)] + ([len(runs)] if len(runs) not in (1, 3, 6, 10, 15) else []):
    groups = [runs[i:i + size] for i in range(0, len(runs) - size + 1, size)]
    es = [effects(g) for g in groups]
    line = f"   size {size:2d} ({len(groups):2d} groups) "
    for key in KEYS:
        ws = [width(e[key]) for e in es if width(e[key]) is not None]
        safe = sum(1 for e in es if e[key].hi is not None and e[key].hi <= 1 + MARGIN)
        if ws:
            group_medians[key][size] = st.median(ws)
            line += f" {key.split('/')[0][2:5]}/{key.split('/')[1][:3]} {st.median(ws):.2f} [{min(ws):.2f}-{max(ws):.2f}] safe {safe}/{len(groups)};"
        else:
            line += f" {key.split('/')[0][2:5]}/{key.split('/')[1][:3]} n/a (too few samples);"
    all_safe = sum(1 for e in es if all(e[key].hi is not None and e[key].hi <= 1 + MARGIN for key in KEYS))
    print(line + f"  ALL SAFE {all_safe}/{len(groups)}")
print(" duration (each run truncated to its first d seconds after warm-up), median width over groups:")
for size in (1, 3):
    groups = [runs[i:i + size] for i in range(0, len(runs) - size + 1, size)]
    for d in (20.0, 35.0, None):
        es = [effects(g, trunc=d) for g in groups]
        line = f"   k={size} d={'full' if d is None else int(d):>4} "
        for key in KEYS:
            ws = [width(e[key]) for e in es if width(e[key]) is not None]
            line += f" {key.split('/')[0][2:5]}/{key.split('/')[1][:3]} {fmt(st.median(ws)) if ws else '  n/a'} ({len(ws)}/{len(groups)});"
        print(line)

# ---- A4 between-arm variation
print("\n== A4 between-arm variation ==")


def per_group(size):
    groups = [runs[i:i + size] for i in range(0, len(runs) - size + 1, size)]
    return groups, [effects(g, alpha=0.05) for g in groups], [effects(g) for g in groups]


def med_ratio(group, key):
    p = pooled(group)
    c = [s[1] for s in p["control"].get(tuple(key.split("/")), [])]
    t = [s[1] for s in p["treatment"].get(tuple(key.split("/")), [])]
    return st.median(t) / st.median(c) if c and t else None


verdict_b, cover95_all, cover_eng_all, order_t = {}, [], [], {}
logs_by_key = {}
for key in KEYS:
    level = 1
    groups, e95, eeng = per_group(1)
    if sum(1 for e in e95 if e[key].ratio is not None and e[key].lo is not None) < max(4, len(groups) // 2):
        level = 3
        groups, e95, eeng = per_group(3)
    rows = [(g, a[key], b[key]) for g, a, b in zip(groups, e95, eeng) if a[key].lo is not None]
    if len(rows) < 3:
        print(f" {key}: too few measured groups")
        continue
    lr = [math.log(a.ratio) for _, a, _ in rows]
    se = [(math.log(a.hi) - math.log(a.lo)) / 3.92 for _, a, _ in rows]
    c95 = sum(1 for _, a, _ in rows if a.lo <= 1 <= a.hi) / len(rows)
    ceng = sum(1 for _, _, b in rows if b.lo <= 1 <= b.hi) / len(rows)
    cal = st.stdev(lr) / st.median(se)
    verdict_b[key] = cal
    cover95_all += [a.lo <= 1 <= a.hi for _, a, _ in rows]
    cover_eng_all += [b.lo <= 1 <= b.hi for _, _, b in rows]
    if level == 1:
        logs_by_key[key] = {g[0]["run"]: math.log(a.ratio) for g, a, _ in rows}
        second_first = [(-x if g[0]["treatment_first"] else x) for (g, _, _), x in zip(rows, lr)]
        t_stat = st.mean(second_first) / (st.stdev(second_first) / math.sqrt(len(second_first)))
        order_t[key] = t_stat
        order = f"order ln(second/first) mean {st.mean(second_first):+.3f} t {t_stat:+.2f}"
    else:
        order = "order n/a at triplet level (arms alternate inside a triplet)"
    mr = [math.log(m) for m in (med_ratio(g, key) for g, _, _ in rows) if m]
    print(f" {key:18s} level={'run' if level == 1 else 'triplet'} n={len(rows):2d}  sd ln(p95 ratio) {st.stdev(lr):.3f}  median boot SE {st.median(se):.3f}"
          f"  calibration {cal:.2f}  cover95 {c95:.0%}  cover(engine alpha) {ceng:.0%}  ratio range {math.exp(min(lr)):.2f}-{math.exp(max(lr)):.2f}"
          f"  sd ln(median ratio) {st.stdev(mr):.3f}  | {order}")
if not cover95_all:
    sys.exit("too few runs for A4/A5")
cov95 = sum(cover95_all) / len(cover95_all)
print(f" (a) overall 95% coverage {cov95:.0%} ({sum(cover95_all)}/{len(cover95_all)}); at engine alpha {sum(cover_eng_all) / len(cover_eng_all):.0%}")
print(" (c) correlation across runs between keys, ln(p95 ratio) [run-level keys only]:")
rk = [k for k in KEYS if k in logs_by_key]
common = sorted(set.intersection(*[set(logs_by_key[k]) for k in rk])) if rk else []
if len(common) >= 4:
    m = np.corrcoef(np.array([[logs_by_key[k][i] for i in common] for k in rk]))
    for i, k in enumerate(rk):
        print(f"    {k:18s} " + " ".join(f"{m[i, j]:+.2f}" for j in range(len(rk))))
    off = [m[i, j] for i in range(len(rk)) for j in range(i + 1, len(rk))]
    print(f"    mean off-diagonal correlation {st.mean(off):+.2f}")

print(" (e) paired shift: per run, median ln(latency in arm run second / first) over requests paired by tenant, class and offset:")
shifts, by_key = [], {k: [] for k in KEYS}
for r in runs:
    first, second = ("treatment", "control") if r["treatment_first"] else ("control", "treatment")
    w = warmup(r)
    allv = []
    for key in KEYS:
        a = {}
        for off, lat in r["result"]["arms"][first]["samples"].get(key, []):
            if off >= w:
                a.setdefault(off, []).append(lat)
        v = []
        for off, lat in r["result"]["arms"][second]["samples"].get(key, []):
            if off >= w and a.get(off):
                v.append(math.log(lat / a[off].pop(0)))
        if len(v) >= 10:
            by_key[key].append(st.median(v))
        allv += v
    shifts.append(st.median(allv))
print("    per run (all keys): " + " ".join(f"{x:+.3f}" for x in shifts))
print(f"    all keys: mean {st.mean(shifts):+.3f}  sd {st.stdev(shifts) if len(shifts) > 1 else float('nan'):.3f}  range {min(shifts):+.3f}..{max(shifts):+.3f}"
      f"  (as ratios {math.exp(min(shifts)):.3f}..{math.exp(max(shifts)):.3f})")
for key in KEYS:
    v = by_key[key]
    if len(v) > 2:
        print(f"    {key:18s} mean {st.mean(v):+.3f} sd {st.stdev(v):.3f} range {min(v):+.3f}..{max(v):+.3f}")
shift_keys = [k for k in KEYS if len(by_key[k]) == len(runs)]
if len(shift_keys) >= 2 and len(runs) >= 4:
    m = np.corrcoef(np.array([by_key[k] for k in shift_keys]))
    off = [m[i, j] for i in range(len(shift_keys)) for j in range(i + 1, len(shift_keys))]
    print(f"    correlation of per-key paired shifts across runs, mean off-diagonal {st.mean(off):+.2f}")

many = sum(1 for c in verdict_b.values() if c > 1.5)
if many > len(verdict_b) / 2 or cov95 < 0.85 or any(abs(t) > 3 for t in order_t.values()):
    reading = "VISIBLE"
elif all(c <= 1.25 for c in verdict_b.values()) and cov95 >= 0.90:
    reading = "NOT VISIBLE"
else:
    reading = "UNCLEAR"
print(f" FROZEN READING of the between-arm problem: {reading}  (calibration>1.5 in {many}/{len(verdict_b)} keys; 95% coverage {cov95:.0%};"
      f" max |order t| {max((abs(t) for t in order_t.values()), default=float('nan')):.2f})")

# ---- A5 budget
print("\n== A5 replay budget (fit width = a * k^-b on median widths of disjoint groups; extrapolation) ==")
for key in KEYS:
    pts = sorted(group_medians[key].items())
    if len(pts) < 3:
        print(f" {key}: too few points")
        continue
    x, y = np.log([p[0] for p in pts]), np.log([p[1] for p in pts])
    slope, intercept = np.polyfit(x, y, 1)
    k_needed = math.exp((math.log(2 * MARGIN) - intercept) / slope) if slope < 0 else float("inf")
    print(f" {key:18s} widths " + " ".join(f"k{p[0]}:{p[1]:.2f}" for p in pts) + f"   exponent {slope:+.2f}   replays for width {2 * MARGIN:.2f}: {k_needed:.0f}"
          f"  (~{k_needed * 2 * 2.1:.0f} min of twin time at ~2.1 min per arm)")
