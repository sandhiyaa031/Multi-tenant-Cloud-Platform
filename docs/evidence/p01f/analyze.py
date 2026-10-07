"""P0.1e analysis: applies the frozen criteria in results/p01e/criteria.md. Run from the repository root:
    python results/p01e/analyze.py
Reads run_L*.json, monitor_L*.log, state_L*_{start,end}.txt. Prints the verdict; changes nothing."""
import json
import math
import re
import statistics as st
from pathlib import Path

D = Path(__file__).parent
LEVELS = [10, 20, 30, 40]
WARM, BLOCK = 120.0, 30.0
JUDGED = [("t_steady", "OLTP"), ("t_bursty", "OLTP"), ("t_mixed", "OLTP"), ("t_analytic", "OLAP")]
T_CRIT_DF8 = 2.306  # two-sided 95 %, 10 blocks


def pct(values, p):
    values = sorted(values)
    return values[max(1, math.ceil(p / 100 * len(values))) - 1]


def blocks(run, tenant, cls):
    t0 = run["start_wall"] + WARM
    n = int((run["duration_s"] - WARM) // BLOCK)
    out = []
    for b in range(n):
        lo, hi = t0 + b * BLOCK, t0 + (b + 1) * BLOCK
        v = [s[4] for s in run["samples"] if s[1] == tenant and s[2] == cls and lo <= s[0] < hi]
        out.append((lo + BLOCK / 2 - run["start_wall"], v))
    return out


def stationarity(run, tenant, cls):
    bl = blocks(run, tenant, cls)
    per = [len(v) for _, v in bl]
    xs = [t for (t, v) in bl]
    ys = [pct(v, 95) for (_, v) in bl if v]
    xs = [t for (t, v) in bl if v]
    n = len(ys)
    mx, my = st.mean(xs), st.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    resid = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys))
    se = math.sqrt(resid / (n - 2) / sxx) if n > 2 else float("inf")
    t = slope / se if se else 0.0
    change180 = slope * 180 / my
    significant = abs(t) > T_CRIT_DF8
    trend_bad = significant and abs(change180) > 0.05
    med = st.median(ys)
    break_bad = max(ys) > 3 * med
    return dict(mean_block_p95=my, change_per_180s=change180, t=t, trend_fail=trend_bad, regime_break=break_bad,
                max_over_median=max(ys) / med, avg_samples_per_block=st.mean(per), blocks=n)


def monitor(level, run):
    cpu = {"dp": [], "pgb": [], "gen": [], "col": []}
    act = lock = lw = io = 0
    for line in (D / f"monitor_L{level}.log").read_text().splitlines():
        m = re.match(r"(\S+) ", line)
        if not m:
            continue
        import datetime as dt
        ts = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
        if ts < run["start_wall"] + WARM or ts > run["end_wall"]:
            continue
        for key, pat in (("dp", "dp-primary-1"), ("pgb", "pgbouncer-1"), ("gen", "evaluation-run"), ("col", "collector-1")):
            mm = re.search(pat + r"[0-9a-f-]*=([\d.]+)%", line)
            if mm:
                cpu[key].append(float(mm.group(1)))
        for mm in re.finditer(r"WAIT active=(\d+) lock=(\d+) lwlock=(\d+) io=(\d+)", line):
            a, l, w, i = map(int, mm.groups())
            act += a; lock += l; lw += w; io += i
    return cpu, dict(active=act, lock=lock, lwlock=lw, io=io, lock_share=(lock / act if act else 0.0))


def state(level, which):
    rows = {}
    for line in (D / f"state_L{level}_{which}.txt").read_text().splitlines():
        m = re.match(r"ROWS (\S+) (\d+)", line)
        if m:
            rows[m.group(1)] = int(m.group(2))
    return rows


verdicts, newp = {}, {}
for L in LEVELS:
    f = D / f"run_L{L}.json"
    if not f.exists():
        verdicts[L] = dict(ran=False)
        continue
    run = json.loads(f.read_text())
    summ = run["summary"]
    errs = sum(r["errors"] for r in summ)
    drops = sum(r["dropped"] for r in summ)
    completed = sum(r["completed"] for r in summ)
    no = [s[4] for s in run["samples"] if s[1] == "t_steady" and s[3] == "new_order" and s[0] >= run["start_wall"] + WARM]
    newp[L] = pct(no, 95) if no else float("nan")
    stn = {f"{t}/{c}": stationarity(run, t, c) for t, c in JUDGED}
    cpu, wait = monitor(L, run)
    dp = [x for x in cpu["dp"] if x > 2]
    dp_med = st.median(dp) if dp else float("nan")
    dp_p95 = pct(dp, 95) if dp else float("nan")
    col_med = st.median(cpu["col"]) if cpu["col"] else float("nan")
    s0, s1 = state(L, "start"), state(L, "end")
    growth = {k: (s1[k] - s0[k]) / s0[k] for k in s0 if k in s1 and s0[k]}
    max_growth = max(growth.values()) if growth else float("nan")
    c = {
        "S1 no errors/drops": errs == 0 and drops == 0,
        "S2 stationary (4 judged keys)": all((not v["trend_fail"]) and (not v["regime_break"]) for v in stn.values()),
        "S3 CPU median 55-80% and p95<=95%": 220 <= dp_med <= 320 and dp_p95 <= 380,
        "S4 lock-wait share <=5%": wait["lock_share"] <= 0.05,
        "S5 growth <=5%": max_growth <= 0.05,
        "S6 collector CPU <=80%": col_med <= 80,
    }
    verdicts[L] = dict(ran=True, completed=completed, errors=errs, dropped=drops, stn=stn, dp_med=dp_med, dp_p95=dp_p95,
                       col_med=col_med, pgb_med=st.median(cpu["pgb"]) if cpu["pgb"] else float("nan"),
                       gen_med=st.median(cpu["gen"]) if cpu["gen"] else float("nan"), wait=wait, max_growth=max_growth,
                       growth=growth, criteria=c, qualifies=all(c.values()))

print("P0.1e RESULTS (frozen criteria in criteria.md)\n")
for L in LEVELS:
    v = verdicts[L]
    if not v["ran"]:
        print(f"L{L}: NOT RUN"); continue
    print(f"=== level {L} qps  completed={v['completed']} errors={v['errors']} dropped={v['dropped']}  qualifies={v['qualifies']}")
    print(f"   dp-primary CPU median {v['dp_med']:.0f}% (of 400)  p95 {v['dp_p95']:.0f}%  | pgbouncer med {v['pgb_med']:.0f}%  generator med {v['gen_med']:.0f}%  collector med {v['col_med']:.0f}%")
    print(f"   lock-wait share {v['wait']['lock_share']:.3f} (active snapshots {v['wait']['active']}, lock {v['wait']['lock']}, lwlock {v['wait']['lwlock']}, io {v['wait']['io']}) | max partition growth {v['max_growth']:.2%}")
    for k, s in v["stn"].items():
        print(f"   {k:16} block p95 mean {s['mean_block_p95']:8.1f} ms  change/180s {s['change_per_180s']:+.1%} (t={s['t']:+.1f}) max/median {s['max_over_median']:.2f}  trend_fail={s['trend_fail']} break={s['regime_break']}  ~{s['avg_samples_per_block']:.0f} req/block")
    print("   criteria:", {k: ("PASS" if ok else "FAIL") for k, ok in v["criteria"].items()})
print("\nNew-Order p95 (t_steady, measured window) by level:", {L: round(newp[L], 1) for L in newp})
ran = [L for L in LEVELS if verdicts[L]["ran"]]
steps = {}
for a, b in zip(ran, ran[1:]):
    steps[b] = newp[b] / newp[a]
print("step ratios:", {k: round(v, 2) for k, v in steps.items()})
selected = None
for L in reversed(ran):
    v = verdicts[L]
    if not v["qualifies"]:
        continue
    chain = [x for x in ran if x <= L]
    mono = all(0.9 <= newp[b] / newp[a] <= 2.0 for a, b in zip(chain, chain[1:]))
    visible = newp[L] / newp[ran[0]] >= 1.25
    print(f"candidate level {L}: dose-response ok={mono}, contention visible (New-Order p95 vs L{ran[0]}) = {newp[L]/newp[ran[0]]:.2f}x -> {visible}")
    if mono and visible:
        selected = L
        break
print("\nVERDICT:", f"stable contended operating point exists: level {selected} qps" if selected else
      "NO level meets all frozen criteria with contention visible")
