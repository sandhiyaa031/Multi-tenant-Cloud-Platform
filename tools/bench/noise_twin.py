"""Part 2 of docs/evidence/bench_noise/protocol.md: the twin's A/A noise per procedure, and the
choice between them by the rule fixed in the protocol.

Run inside the bench-tools container:

    python /tools/bench/noise_twin.py /results/bench_noise/part2
"""
import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402

MODES = ("sequential", "simultaneous", "interleaved")


def power_changes(path: Path) -> list[float]:
    if not path.exists():
        return []
    rows = [(float(r[0]), r[1]) for r in csv.reader(path.open(encoding="utf-8")) if len(r) >= 2 and r[0][:1].isdigit()]
    return [at for (_, before), (at, after) in zip(rows, rows[1:]) if before != after]


def invalid(record: dict, changes: list[float]) -> str | None:
    if record["state"] != "DONE":
        return f"the run failed: {record.get('error', '')[:100]}"
    result = record["result"]
    errors = sum(sum(arm["errors"].values()) for arm in result["arms"].values())
    if errors > 0.01 * 2 * max(result["transactions"], 1):
        return f"replay errors {errors} of {2 * result['transactions']}"
    if any(record["started"] <= at <= record["finished"] for at in changes):
        return "the power source changed during it"
    return None


def pair_ratios(result: dict) -> dict[str, float]:
    """ln(treatment / control) for class-level p50/p95/p99 and per-template p50/p95 of one pair."""
    control, treatment = result["arms"]["control"], result["arms"]["treatment"]
    warmup = min(15.0, result["window_s"] * 0.15)                  # as the engine does before judging
    out = {}
    for key in sorted(set(control["samples"]) & set(treatment["samples"])):
        c = [latency for offset, latency in control["samples"][key] if offset >= warmup]
        t = [latency for offset, latency in treatment["samples"][key] if offset >= warmup]
        if min(len(c), len(t)) < 10:                               # the gate's min_pair_samples
            continue
        for label, p in (("p50", 50), ("p95", 95), ("p99", 99)):
            out[f"class:{key}:{label}"] = math.log(float(np.percentile(t, p)) / float(np.percentile(c, p)))
        for qid in sorted(set(control["templates"].get(key, {})) & set(treatment["templates"].get(key, {}))):
            a, b = control["templates"][key][qid], treatment["templates"][key][qid]
            if min(a["n"], b["n"]) >= metrics.MIN_TEMPLATE_N and a["p95_ms"] > 0 and b["p95_ms"] > 0:
                out[f"template:{key}#{qid}:p95"] = math.log(b["p95_ms"] / a["p95_ms"])
                out[f"template:{key}#{qid}:p50"] = math.log(b["p50_ms"] / a["p50_ms"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    args = parser.parse_args()
    directory = Path(args.directory)
    changes = power_changes(directory / "power.csv")

    ratios = {mode: {} for mode in MODES}
    counts = {mode: [0, 0, 0.0] for mode in MODES}                 # valid, total, seconds of valid pairs
    excluded, texts = [], {}
    for path in sorted(directory.glob("round_*_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        mode = record["mode"]
        counts[mode][1] += 1
        why = invalid(record, changes)
        if why:
            excluded.append((path.name, why))
            continue
        counts[mode][0] += 1
        counts[mode][2] += record["finished"] - record["started"]
        for arm in record["result"]["arms"].values():
            for by_template in arm["templates"].values():
                texts.update({qid: row["sql"] for qid, row in by_template.items()})
        for name, value in pair_ratios(record["result"]).items():
            ratios[mode].setdefault(name, []).append(value)

    tables = {mode: {name: row for name, values in sorted(ratios[mode].items()) if (row := metrics.noise(values))} for mode in MODES}
    medians = {}
    for mode in MODES:
        sds = [row["sd_log"] for name, row in tables[mode].items() if name.startswith("class:") and name.endswith(":p95")]
        medians[mode] = statistics.median(sds) if sds else None

    # The rule fixed in the protocol before any of this was measured.
    sim, inter, seq = medians["simultaneous"], medians["interleaved"], medians["sequential"]
    if sim is None or inter is None:
        adopted, reason = None, "a procedure has no valid pairs; no choice can be made"
    elif seq is not None and seq < sim and seq < inter:
        adopted, reason = "sequential", "both new procedures have a higher median than sequential"
    elif abs(sim - inter) <= 0.10 * max(sim, inter) or sim < inter:
        adopted = "simultaneous"
        reason = "the two medians are within 10% of each other" if abs(sim - inter) <= 0.10 * max(sim, inter) else "it has the lower median"
    else:
        adopted, reason = "interleaved", "it has the lower median"

    out = {"counts": {m: {"valid": c[0], "total": c[1], "mean_seconds_per_pair": c[2] / c[0] if c[0] else None} for m, c in counts.items()},
           "excluded": [{"pair": n, "why": w} for n, w in excluded], "noise": tables, "median_sd_class_p95": medians,
           "adopted": adopted, "reason": reason, "templates": texts}
    (directory / "noise_twin.json").write_text(json.dumps(out, indent=1), encoding="utf-8")

    lines = ["PART 2: twin A/A by procedure"]
    for mode in MODES:
        valid, total, seconds = counts[mode]
        lines.append(f"  {mode:13s} {valid} of {total} pairs valid" + (f", {seconds / valid:.0f} s per pair" if valid else ""))
    lines += [f"  EXCLUDED {name}: {why}" for name, why in excluded]
    lines.append("")
    lines.append(f"{'metric':52s} " + " ".join(f"{mode + ': sd [95%] MDE10':>36s}" for mode in MODES))
    names = sorted({name for mode in MODES for name in tables[mode]}, key=lambda n: (not n.startswith("class:"), n))
    for name in names:
        cells = []
        for mode in MODES:
            row = tables[mode].get(name)
            cells.append(f"{row['sd_log']:.4f} [{row['sd_log_lo']:.3f}, {row['sd_log_hi']:.3f}] {row['mde10_pct']:5.1f}% n={row['n']:<2d}"
                         if row else "-")
        lines.append(f"{name:52s} " + " ".join(f"{cell:>36s}" for cell in cells))
    lines.append("")
    lines.append("median over class-level keys of the sd of ln(p95 ratio): "
                 + ", ".join(f"{mode} {medians[mode]:.4f}" if medians[mode] is not None else f"{mode} -" for mode in MODES))
    lines.append(f"ADOPTED: {adopted} ({reason})")
    (directory / "noise_twin.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if all(c[0] >= 10 for c in counts.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
