"""P0.1h soak analysis. Implements results/p01h/decision_rules.md exactly. Usage: python results/p01h/analyze.py [dir]
Reads markers.log, canary22.log, canary23.log, host.jsonl. Prints tables and the frozen verdicts. Changes nothing."""
import datetime as dt
import json
import re
import statistics as st
import sys
from pathlib import Path

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
BASE_LO, BASE_HI, WIN = 60, 300, 60  # baseline = soak seconds 60-300; windows of 60 s afterwards
CAN_BAND, FREQ_DROP, ROOT_RISE, PLACE_SHIFT, NEED = 0.10, 0.07, 0.75, 0.15, 3


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def med(v):
    return st.median(v) if v else float("nan")


def mean(v):
    return st.mean(v) if v else float("nan")


marks = {}
for line in (D / "markers.log").read_text().splitlines():
    t, rest = line.split(" ", 1)
    for key in ("PHASE idle", "PHASE soak", "PHASE rest", "PHASE reburn begins", "PHASE reburn ends", "END"):
        if rest.startswith(key):
            marks[key] = ts(t)
soak0, soak1 = marks["PHASE soak"], marks["PHASE rest"]
rest1, reb0, reb1 = marks["PHASE reburn begins"], marks["PHASE reburn begins"], marks["PHASE reburn ends"]
idle0 = marks["PHASE idle"]


def canary(name):
    out = []
    for line in (D / name).read_text().splitlines():
        p = line.split()
        if len(p) == 2 and p[1].isdigit():
            out.append((ts(p[0]), int(p[1])))
    return out


def in_win(series, a, b):
    return [v for t, v in series if a <= t < b]


# ---- host samples
host = [json.loads(l) for l in (D / "host.jsonl").read_text().splitlines() if l.strip()]
for h in host:
    h["ts"] = ts(h["t"])
LP = re.compile(r"hv lp (\d+)\)\\% idle time")
AF = re.compile(r"processor information\((\d+),(\d+)\)\\actual frequency")
PL = re.compile(r"processor information\((\d+),(\d+)\)\\% performance limit")
PF = re.compile(r"processor information\((\d+),(\d+)\)\\performance limit flags")
ROOT = re.compile(r"root virtual processor\(root vp (\d+)\)\\% total run time")


def sample_metrics(h):
    c = h["c"]
    busy, freq, plim, pflag, root = {}, {}, {}, {}, 0.0
    for k, v in c.items():
        m = LP.search(k)
        if m: busy[int(m.group(1))] = max(0.0, 100 - v)
        m = AF.search(k)
        if m: freq[int(m.group(2))] = v
        m = PL.search(k)
        if m: plim[int(m.group(2))] = v
        m = PF.search(k)
        if m: pflag[int(m.group(2))] = v
        m = ROOT.search(k)
        if m: root += v / 100
    hot = [i for i, b in busy.items() if b > 50]
    r = {
        "hot": len(hot),
        "busy_freq": mean([freq[i] for i in hot if i in freq]),
        "limited": mean([1.0 if (plim.get(i, 100) < 100 or pflag.get(i, 0) != 0) else 0.0 for i in hot]) if hot else float("nan"),
        "plim": mean([plim[i] for i in hot if i in plim]),
        "root": root,
        "p0": sum(b for i, b in busy.items() if i <= 15 and i % 2 == 0) / 100,
        "sib": sum(b for i, b in busy.items() if i <= 15 and i % 2 == 1) / 100,
        "e": sum(b for i, b in busy.items() if i >= 16) / 100,
        "throttle": max([v for k, v in c.items() if "throttle reasons" in k] or [0]),
        "temp": max([v for k, v in c.items() if k.endswith("\\temperature")] or [float("nan")]),
        "power": mean([v for k, v in c.items() if "power meter(_total)" in k]),
        "ac": h["ac"],
    }
    tot = r["p0"] + r["sib"] + r["e"]
    r["spill"] = (r["sib"] + r["e"]) / tot if tot > 0 else float("nan")
    return r


for h in host:
    h["m"] = sample_metrics(h)


def hwin(a, b, key):
    return [h["m"][key] for h in host if a <= h["ts"] < b and h["m"][key] == h["m"][key]]


def consecutive(flags, n=NEED):
    run = 0
    for f in flags:
        run = run + 1 if f else 0
        if run >= n:
            return True
    return False


print("P0.1h SOAK RESULTS (frozen rules in decision_rules.md)")
print(f"phases (UTC): idle {dt.datetime.fromtimestamp(idle0, dt.timezone.utc):%H:%M:%S}, soak {dt.datetime.fromtimestamp(soak0, dt.timezone.utc):%H:%M:%S}-{dt.datetime.fromtimestamp(soak1, dt.timezone.utc):%H:%M:%S} ({(soak1-soak0)/60:.0f} min), reburn {(reb1-reb0)/60:.1f} min; host samples {len(host)}")

# ---- D1 canary
verdict_d1 = {}
for name in ("canary22.log", "canary23.log"):
    cs = canary(name)
    base = med(in_win(cs, soak0 + BASE_LO, soak0 + BASE_HI))
    idle_med = med(in_win(cs, idle0, soak0))
    rest_med = med(in_win(cs, soak1 + 120, rest1))
    reb_med = med(in_win(cs, reb0 + BASE_LO, reb1))
    wins = []
    t = soak0 + BASE_HI
    while t + WIN <= soak1:
        wins.append((t, med(in_win(cs, t, t + WIN))))
        t += WIN
    ratios = [w / base for _, w in wins if w == w]
    degraded = consecutive([r > 1 + CAN_BAND for r in ratios])
    verdict_d1[name] = dict(base=base, idle=idle_med, rest=rest_med, reburn=reb_med, ratios=ratios, degraded=degraded,
                            maxr=max(ratios) if ratios else float("nan"), last5=med(ratios[-5:]) if ratios else float("nan"))
    print(f"\n[D1] {name}: idle median {idle_med:.0f} ms | soak baseline (s 60-300) {base:.0f} ms | rest median {rest_med:.0f} ms | reburn (s 60+) {reb_med:.0f} ms (ratio {reb_med/base:.2f})")
    print("     60-s window ratios vs baseline (minutes 6..35): " + " ".join(f"{r:.2f}" for r in ratios))
    print(f"     max ratio {verdict_d1[name]['maxr']:.2f}, last-5-window median {verdict_d1[name]['last5']:.2f}; >1.10 for >=3 consecutive windows: {degraded}")
d1_degraded = any(v["degraded"] for v in verdict_d1.values())
d1_reburn_ok = all(abs(v["reburn"] / v["base"] - 1) <= CAN_BAND for v in verdict_d1.values())

# ---- per-window host table + D2/D4/D5
base_lo, base_hi = soak0 + BASE_LO, soak0 + BASE_HI
B = {k: mean(hwin(base_lo, base_hi, k)) for k in ("busy_freq", "limited", "plim", "root", "spill", "temp", "power", "hot")}
print("\n[host] baseline (s 60-300): busy-LP freq {busy_freq:.0f} MHz | limited share {limited:.2f} | %perf-limit {plim:.0f} | root(host) core-eq {root:.2f} | spill share (SMT sibling+E) {spill:.2f} | temp max {temp:.0f} | power {power:.0f} | busy LPs {hot:.1f}".format(**B))
print("       window(min) freq MHz  limited  plim  root  spill  temp  throttle power  busyLPs")
freq_flags, root_flags, spill_flags, lim_flags, thr_flags = [], [], [], [], []
t = soak0 + BASE_HI
while t + WIN <= soak1:
    w = {k: mean(hwin(t, t + WIN, k)) for k in ("busy_freq", "limited", "plim", "root", "spill", "temp", "power", "hot")}
    thr = max(hwin(t, t + WIN, "throttle") or [0])
    freq_flags.append(w["busy_freq"] < B["busy_freq"] * (1 - FREQ_DROP))
    root_flags.append(w["root"] >= B["root"] + ROOT_RISE)
    spill_flags.append(abs(w["spill"] - B["spill"]) > PLACE_SHIFT)
    lim_flags.append((w["limited"] >= 0.25 and B["limited"] < 0.10) or thr != 0)
    thr_flags.append(thr != 0)
    print(f"       {(t-soak0)/60:5.0f}     {w['busy_freq']:7.0f}  {w['limited']:6.2f} {w['plim']:5.0f} {w['root']:5.2f} {w['spill']:5.2f} {w['temp']:5.0f} {thr:6.0f} {w['power']:8.0f} {w['hot']:6.1f}")
    t += WIN
d2_freq = consecutive(freq_flags)
d2_limit = consecutive(lim_flags)
d4 = consecutive(root_flags)
d5 = consecutive(spill_flags)

# ---- D3 AC
acs = [(h["ts"], h["ac"]) for h in host]
off = [(a, b) for a, b in acs if b != "Online"]
ac_stable = not off
print(f"\n[D3] AC samples: {len(acs)}; non-Online samples: {len(off)}" + (f" (first at {(off[0][0]-idle0)/60:.1f} min after idle start, last at {(off[-1][0]-idle0)/60:.1f} min)" if off else ""))
print(f"[D2] busy-LP actual frequency down >{FREQ_DROP:.0%} for >=3 consecutive windows: {d2_freq}; limit flags/throttle reasons appear: {d2_limit}")
print(f"[D4] host (root) CPU up >= {ROOT_RISE} core-eq vs baseline for >=3 consecutive windows: {d4}")
print(f"[D5] SMT-sibling+E-core share shifts >{PLACE_SHIFT:.0%} points for >=3 consecutive windows: {d5}")

# ---- frozen attribution
if not d1_degraded:
    attribution = "H_none: canary stayed within the +/-10% band; the platform did not slow under this synthetic load"
else:
    hits = []
    if d2_freq or d2_limit: hits.append("thermal/power limiting")
    if (not d2_freq and not d2_limit) and d4: hits.append("host contention")
    if (not d2_freq and not d2_limit) and d5 and not d4: hits.append("hybrid-core placement")
    attribution = ("degraded; best-supported: " + " + ".join(hits)) if hits else "degraded but unexplained by D2/D4/D5"
suitable = (not d1_degraded) and ac_stable and d1_reburn_ok
print(f"\nVERDICT D1 degraded >10%: {d1_degraded} | reburn within +/-10%: {d1_reburn_ok} | AC stable: {ac_stable}")
print("ATTRIBUTION:", attribution)
print("SUITABLE for controlled DBPilot experiments in the current configuration:", suitable)
