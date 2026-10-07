"""E1 load: the harness's base workload (scenario S9_no_change, no overrides) for a fixed time. A thin wrapper
around the unchanged workload.driver.run and evaluation.profile_for.

usage: python /results/e1/load.py <duration_s>
"""
import asyncio
import json
import os
import sys
import time

from evaluation.__main__ import POOL_SIZE, RATE_SCALE, SPEC, profile_for
from workload.driver import Recorder, run

duration = float(sys.argv[1])
profile = profile_for(SPEC["scenarios"]["S9_no_change"])
host, password = os.environ.get("DP_POOLER_HOST", "pgbouncer"), os.environ["DP_TENANT_PASSWORD"]
json.dump({"profile": profile, "rate_scale": RATE_SCALE, "pool_size": POOL_SIZE, "start_wall": time.time(),
           "duration_s": duration}, open("/results/e1/load_meta.json", "w"))
rec = Recorder(out_path="/results/e1/load_ops.jsonl", interval_s=10.0)
summary = asyncio.run(run(profile, lambda role: f"host={host} port=6432 dbname=app user={role} password={password}",
                          duration, None, interval_s=10.0, pool_size=POOL_SIZE, recorder=rec))
json.dump(summary, open("/results/e1/load_summary.json", "w"))
print(json.dumps(summary))
