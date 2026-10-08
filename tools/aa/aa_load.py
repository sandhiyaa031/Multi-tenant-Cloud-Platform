"""A/A validation load: the harness's base workload (scenario S9_no_change, no overrides) for a fixed time.
Runs in the evaluation container. A thin wrapper around workload.driver.run and evaluation.profile_for.

usage: python /tools/aa/aa_load.py <duration_s> <out_dir>
"""
import asyncio
import json
import os
import sys
import time

from evaluation.__main__ import POOL_SIZE, RATE_SCALE, SPEC, profile_for
from workload.driver import Recorder, run

duration, out = float(sys.argv[1]), sys.argv[2]
os.makedirs(out, exist_ok=True)
profile = profile_for(SPEC["scenarios"]["S9_no_change"])
host, password = os.environ.get("DP_POOLER_HOST", "pgbouncer"), os.environ["DP_TENANT_PASSWORD"]
with open(f"{out}/load_meta.json", "w") as f:
    json.dump({"profile": profile, "rate_scale": RATE_SCALE, "pool_size": POOL_SIZE, "start_wall": time.time(),
               "duration_s": duration}, f)
recorder = Recorder(out_path=f"{out}/load_ops.jsonl", interval_s=10.0)
summary = asyncio.run(run(profile, lambda role: f"host={host} port=6432 dbname=app user={role} password={password}",
                          duration, None, interval_s=10.0, pool_size=POOL_SIZE, recorder=recorder))
with open(f"{out}/load_summary.json", "w") as f:
    json.dump(summary, f)
