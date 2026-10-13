"""Part 1 of docs/evidence/bench_noise/protocol.md: the noise floor in production (A/A).

One continuous run with nothing changed is cut into repetitions of three consecutive segments
(before, live, after). Per repetition and metric the bracketed ratio live / mean(before, after)
is formed, exactly as the trial harness forms it, and its spread over the repetitions is the noise.

Run inside the bench-tools container after the run:

    python /tools/bench/noise_prod.py /results/bench_noise/part1 --reps 12
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402
from dbpilot_core.pglog import read_window  # noqa: E402

# (label, where it comes from, field): what is compared between segments.
CLIENT_FIELDS = ("p50", "p95", "p99", "per_s")
SERVER_FIELDS = ("p50", "p95", "p99")


def power_changes(path: Path) -> list[float]:
    """Epochs at which the power source changed, from the host sampler's CSV (epoch,status)."""
    if not path.exists():
        return []
    rows = [(float(r[0]), r[1]) for r in csv.reader(path.open(encoding="utf-8")) if len(r) >= 2 and r[0][:1].isdigit()]
    return [at for (_, before), (at, after) in zip(rows, rows[1:]) if before != after]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--reps", type=int, default=12)
    parser.add_argument("--warmup", type=float, default=150)
    parser.add_argument("--segment", type=float, default=60)
    args = parser.parse_args()
    directory = Path(args.directory)

    ops = metrics.load_ops(directory / "ops.jsonl")
    first = min(op[0] for op in ops)
    begin = first + args.warmup
    end = begin + args.reps * 3 * args.segment
    if max(op[0] for op in ops) < end - 5:
        print("the run is shorter than warm-up plus the repetitions asked for")
        return 1
    as_dt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc)  # noqa: E731
    transactions = read_window(os.environ.get("DP_LOG_DIR", "/var/log/dbpilot"), as_dt(begin - 5), as_dt(end + 30))
    changes = power_changes(directory / "power.csv")
    intervals = [json.loads(line) for line in (directory / "intervals.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()] \
        if (directory / "intervals.jsonl").exists() else []

    ratios: dict[str, list[float]] = {}
    tenants = {op[1] for op in ops}
    excluded, valid = [], 0
    texts: dict[str, str] = {}
    for rep in range(args.reps):
        t0 = begin + rep * 3 * args.segment
        bounds = [(t0 + i * args.segment, t0 + (i + 1) * args.segment) for i in range(3)]
        if any(t0 <= at < bounds[2][1] for at in changes):
            excluded.append((rep, "the power source changed during it"))
            continue
        done = sum(1 for op in ops if t0 <= op[0] < bounds[2][1])
        errors = sum(row.get("errors", 0) + row.get("dropped", 0) for row in intervals
                     if t0 - first <= row["t"] < bounds[2][1] - first + 10)
        if done == 0 or errors > 0.01 * done:
            excluded.append((rep, f"{errors} errors or dropped arrivals against {done} completed"))
            continue
        valid += 1
        client = [metrics.client_metrics(ops, a, b) for a, b in bounds]
        server = []
        for a, b in bounds:
            summary, seen = metrics.server_metrics(transactions, a, b, tenants)
            server.append(summary)
            texts.update(seen)
        for source, parts, fields in (("client", client, CLIENT_FIELDS), ("server", server, SERVER_FIELDS)):
            for key in parts[1]:
                if not all(key in part for part in parts):
                    continue
                if "#" in key and min(part[key]["n"] for part in parts) < metrics.MIN_TEMPLATE_N:
                    continue
                for field in fields:
                    before, live, after = (part[key].get(field) for part in parts)
                    if before and live and after:
                        ratios.setdefault(f"{source}:{key}:{field}", []).append(math.log(live / ((before + after) / 2)))

    table = {name: metrics.noise(values) for name, values in sorted(ratios.items())}
    table = {name: row for name, row in table.items() if row}
    out = {"window": [metrics.iso(begin), metrics.iso(end)], "repetitions": args.reps, "valid": valid,
           "excluded": [{"repetition": r, "why": why} for r, why in excluded], "noise": table, "templates": texts}
    (directory / "noise_prod.json").write_text(json.dumps(out, indent=1), encoding="utf-8")

    lines = [f"PART 1: production A/A, {metrics.iso(begin)} .. {metrics.iso(end)}; {valid} of {args.reps} repetitions valid"]
    lines += [f"  EXCLUDED repetition {r}: {why}" for r, why in excluded]
    lines.append(f"{'metric':58s} {'n':>3s} {'sd of ln ratio':>15s} {'[95% of sd]':>17s} {'mean ratio [95%]':>24s} {'MDE10':>7s}")
    for name, row in table.items():
        lines.append(f"{name:58s} {row['n']:3d} {row['sd_log']:15.4f} [{row['sd_log_lo']:.4f}, {row['sd_log_hi']:.4f}]"
                     f" {row['mean_ratio']:8.3f} [{row['mean_lo']:.3f}, {row['mean_hi']:.3f}] {row['mde10_pct']:6.1f}%")
    (directory / "noise_prod.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if valid >= 10 else 2


if __name__ == "__main__":
    sys.exit(main())
