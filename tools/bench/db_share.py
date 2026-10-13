"""Part 0 of docs/evidence/bench_noise/protocol.md: where a transaction's time goes.

Run inside the bench-tools container, right after one workload run:

    python /tools/bench/db_share.py /results/bench_noise/part0/v2_ops.jsonl --variant v2 \
        --warmup 150 --window 180 --out /results/bench_noise/part0/v2.json

The window starts `warmup` seconds after the first operation in the ops file.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402
from dbpilot_core.pglog import read_window  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ops")
    parser.add_argument("--variant", required=True)
    parser.add_argument("--warmup", type=float, default=150)
    parser.add_argument("--window", type=float, default=180)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    ops = metrics.load_ops(args.ops)
    start = min(op[0] for op in ops) + args.warmup
    end = start + args.window
    if max(op[0] for op in ops) < end - 5:
        print(f"the run is shorter than warm-up plus window: last operation at {metrics.iso(max(op[0] for op in ops))}")
        return 1
    as_dt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc)  # noqa: E731
    # A little either side: a transaction is placed by its start, the log by each statement's end.
    transactions = read_window(os.environ.get("DP_LOG_DIR", "/var/log/dbpilot"), as_dt(start - 5), as_dt(end + 30))
    share = metrics.db_time_share(ops, transactions, start, end)
    client = metrics.client_metrics(ops, start, end)
    server, texts = metrics.server_metrics(transactions, start, end, {op[1] for op in ops})
    out = {"variant": args.variant, "window": [metrics.iso(start), metrics.iso(end)], "db_time_share": share,
           "client": client, "server": server, "templates": texts}
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")

    print(f"variant {args.variant}, window {metrics.iso(start)} .. {metrics.iso(end)}")
    print(f"{'key':34s} {'client n':>8s} {'server n':>8s} {'client ms':>10s} {'exec ms':>9s} {'in-DB span':>10s} {'stmts':>6s} {'DB share':>9s}")
    for key, row in share.items():
        if "db_time_share" not in row:
            print(f"{key:34s} {row['client_n']:8d} {row['server_n']:8d}   (not on both sides)")
            continue
        print(f"{key:34s} {row['client_n']:8d} {row['server_n']:8d} {row['client_mean_ms']:10.2f} {row['exec_mean_ms']:9.2f}"
              f" {row['span_mean_ms']:10.2f} {row['statements_per_txn']:6.1f} {row['db_time_share']:8.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
