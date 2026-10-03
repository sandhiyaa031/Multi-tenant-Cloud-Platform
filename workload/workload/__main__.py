import argparse
import asyncio
import json
import os
import random
import sys

from workload.driver import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="workload", description="DBPilot tenant-aware open-loop workload driver")
    parser.add_argument("--profile", default="profiles/baseline.json")
    parser.add_argument("--duration", type=float, default=60, help="seconds")
    parser.add_argument("--out", help="append per-interval measurements to this JSONL file")
    parser.add_argument("--interval", type=float, default=10, help="measurement interval in seconds")
    parser.add_argument("--seed", type=int, help="make the arrival sequence reproducible")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
    with open(args.profile, encoding="utf-8") as f:
        profile = json.load(f)

    host = os.environ.get("DP_POOLER_HOST", "pgbouncer")
    port = os.environ.get("DP_POOLER_PORT", "6432")
    database = os.environ.get("DP_DATABASE", "app")
    password = os.environ["DP_TENANT_PASSWORD"]

    def dsn_for(role: str) -> str:
        return f"host={host} port={port} dbname={database} user={role} password={password}"

    summary = asyncio.run(run(profile, dsn_for, args.duration, args.out, interval_s=args.interval))

    header = f"{'tenant':<12} {'class':<5} {'done':>7} {'per_s':>7} {'err':>5} {'drop':>5} {'p50':>9} {'p95':>9} {'p99':>9}"
    print(header)
    for r in summary:
        print(
            f"{r['tenant']:<12} {r['class']:<5} {r['completed']:>7} {r['per_s']:>7} {r['errors']:>5}"
            f" {r['dropped']:>5} {r['p50_ms']!s:>9} {r['p95_ms']!s:>9} {r['p99_ms']!s:>9}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
