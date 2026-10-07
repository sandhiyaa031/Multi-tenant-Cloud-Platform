"""P0.1e: one no-action characterization run. A thin wrapper around the unchanged workload.driver.run():
the Recorder instance is wrapped (not modified) so that every completed request, error and drop is
timestamped; class statistics are computed afterwards from the raw samples.

usage: python /results/p01e/run.py <level> <duration_s>
"""
import asyncio
import hashlib
import json
import os
import sys
import time

from workload.driver import Recorder, run

level, duration = sys.argv[1], float(sys.argv[2])
profile_path = f"/results/p01e/profile_L{level}.json"
raw = open(profile_path, "rb").read()
profile = json.loads(raw)
host, password = os.environ.get("DP_POOLER_HOST", "pgbouncer"), os.environ["DP_TENANT_PASSWORD"]

samples, events = [], []
rec = Recorder(out_path=f"/results/p01e/ops_L{level}.jsonl", interval_s=10.0)
_ok, _err, _drop = rec.ok, rec.error, rec.dropped


def ok(key, ms):
    samples.append((time.time(), key[0], key[1], key[2], ms))
    _ok(key, ms)


def err(key):
    events.append((time.time(), "error", key[0], key[1], key[2]))
    _err(key)


def drop(key):
    events.append((time.time(), "dropped", key[0], key[1], key[2]))
    _drop(key)


rec.ok, rec.error, rec.dropped = ok, err, drop
start = time.time()
summary = asyncio.run(run(profile, lambda role: f"host={host} port=6432 dbname=app user={role} password={password}",
                          duration, None, interval_s=10.0, pool_size=16, recorder=rec))
end = time.time()
json.dump({"level_qps": float(level), "start_wall": start, "end_wall": end, "duration_s": duration,
           "profile_sha256": hashlib.sha256(raw).hexdigest(), "container_hostname": os.uname().nodename,
           "summary": summary, "samples": samples, "events": events},
          open(f"/results/p01e/run_L{level}.json", "w"))
print(json.dumps(summary))
