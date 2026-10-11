"""Part A of tools/final/protocol.md: what each gate decides when a known slowdown is injected into
real no-action replay pairs. Semi-synthetic; no new measurement.

usage: python tools/final/power.py <e1 dir> <out dir>
"""
import json
import random
import sys
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))
from dbpilot_core import gate  # noqa: E402

SLOS = {("t_analytic", "OLAP"): (95, 2000.0), ("t_bursty", "OLTP"): (99, 150.0),
        ("t_mixed", "OLTP"): (99, 150.0), ("t_steady", "OLTP"): (99, 100.0)}
BENEFIT, HARMS, BUDGETS, ORDERINGS = 0.10, (0.05, 0.10, 0.20, 0.50, 1.00), (3, 8, 14, 28), 20
RUNS: list[dict] = []
KEYS: list[str] = []


def load(directory: Path) -> None:
    for path in sorted(directory.glob("run_*.json")):
        r = json.loads(path.read_text(encoding="utf-8"))
        res = r.get("result")
        if r.get("state") != "DONE" or not res:
            continue
        errors = sum(sum(a["errors"].values()) for a in res["arms"].values())
        if errors > 0.01 * 2 * max(res["transactions"], 1) or r["power_start"].split("|")[0] != r["power_end"].split("|")[0]:
            continue
        RUNS.append(r)
    KEYS.extend(sorted({k for r in RUNS for a in r["result"]["arms"].values() for k in a["samples"]}))


def decide(subset: list[dict], factors: dict[str, float], target: str, looks: int, mode: str):
    control, treatment, order = {}, {}, {}
    for look, r in enumerate(subset, start=1):
        warmup = min(15.0, r["result"]["window_s"] * 0.15)
        arms = r["result"]["arms"]
        gate.pool(control, {tuple(k.split("/")): [(s[0], s[1]) for s in v] for k, v in arms["control"]["samples"].items()}, look, warmup)
        gate.pool(treatment, {tuple(k.split("/")): [(s[0], s[1] * factors.get(k, 1.0)) for s in v]
                              for k, v in arms["treatment"]["samples"].items()}, look, warmup)
        order[look] = r["treatment_first"]
    return gate.decide(control, treatment, target, gate.GatePolicy(confidence=1 - 0.05 / looks), mode=mode,
                       slo_ms=SLOS, treatment_first=order)


def outcome(group: list[dict], factors: dict[str, float], target: str, mode: str) -> str:
    """Engine.verify: pairs are added until the verdict is not INCONCLUSIVE."""
    for look in range(1, len(group) + 1):
        v = decide(group[:look], factors, target, len(group), mode)
        if v.decision != "INCONCLUSIVE":
            break
    if v.decision == "REJECT":
        return "REJECT_HARM" if any(x.startswith("harm shown") for x in v.reasons) else "REJECT_NO_BENEFIT"
    return v.decision


def cell(args) -> dict:
    directory, target, victim, harm, budget, without = args
    if not RUNS:
        load(Path(directory))
    runs = [r for r in RUNS if r["run"] != without]
    factors = {f"{target}/OLAP": BENEFIT}
    if victim:
        factors[victim] = 1 + harm
    rng = random.Random(f"{target}|{victim}|{harm}|{budget}")
    counts = {"per_tenant": Counter(), "aggregate": Counter()}
    n = 0
    for _ in range(ORDERINGS):
        order = runs[:]
        rng.shuffle(order)
        for i in range(0, len(order) - budget + 1, budget):
            group = order[i:i + budget]
            n += 1
            for mode in counts:
                counts[mode][outcome(group, factors, target, mode)] += 1
    return {"target": target, "victim": victim, "harm": harm, "budget": budget, "without_run": without, "sequences": n,
            **{mode: dict(c) for mode, c in counts.items()}}


def main() -> None:
    directory, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    load(directory)
    if "--render" in sys.argv:  # write the table again from cells already computed
        return render(json.loads((out / "power.json").read_text(encoding="utf-8"))["cells"], out)
    jobs = []
    for without in (None, 22):
        for target in ("t_analytic", "t_mixed"):
            neighbours = [k for k in KEYS if not k.startswith(target + "/")]
            for budget in BUDGETS:
                jobs.append((str(directory), target, None, 0.0, budget, without))
                jobs += [(str(directory), target, v, h, budget, without) for v in neighbours for h in HARMS]
    with Pool() as pool:
        cells = pool.map(cell, jobs, chunksize=4)
    (out / "power.json").write_text(json.dumps({"runs": len(RUNS), "benefit": BENEFIT, "orderings": ORDERINGS, "cells": cells}),
                                    encoding="utf-8")

    render(cells, out)


def render(cells: list[dict], out: Path) -> None:
    def pct(c: dict, key: str, n: int) -> str:
        return f"{100 * c.get(key, 0) / n:3.0f}"

    lines = [f"PART A: injected effects on {len(RUNS)} no-action replay pairs (semi-synthetic). Target's OLAP class x{BENEFIT}.",
             "Per cell: % of sequences ending APPROVE / REJECT for harm / REJECT no benefit / INCONCLUSIVE.", ""]
    for without in (None, 22):
        lines.append("=" * 100)
        lines.append("ALL PAIRS" if without is None else "WITHOUT THE STALLED PAIR (run 22)")
        for target in ("t_analytic", "t_mixed"):
            for budget in BUDGETS:
                rows = [c for c in cells if c["without_run"] == without and c["target"] == target and c["budget"] == budget]
                if not rows or rows[0]["sequences"] == 0:
                    continue  # fewer pairs than the budget (27 pairs cannot fill a group of 28)
                lines.append(f"\ntarget {target}, budget {budget} pairs, {rows[0]['sequences']} sequences"
                             f"{'':8s}per-tenant gate: APP REJ-H REJ-NB INC   |   aggregate gate: APP REJ-H REJ-NB INC")
                for c in sorted(rows, key=lambda c: (c["victim"] or "", c["harm"])):
                    n = c["sequences"]
                    label = "no harm" if not c["victim"] else f"{c['victim']} +{c['harm']:.0%}"
                    p, a = c["per_tenant"], c["aggregate"]
                    lines.append(f"   {label:26s}{'':22s}{pct(p, 'APPROVE', n)}  {pct(p, 'REJECT_HARM', n)}    {pct(p, 'REJECT_NO_BENEFIT', n)}   "
                                 f"{pct(p, 'INCONCLUSIVE', n)}   |{'':19s}{pct(a, 'APPROVE', n)}  {pct(a, 'REJECT_HARM', n)}    "
                                 f"{pct(a, 'REJECT_NO_BENEFIT', n)}   {pct(a, 'INCONCLUSIVE', n)}")
    (out / "power.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
