"""
backend/scripts/run_phase3_experiment.py

Phase 3 A/B experiment:
  A: async analytical worker -> primary (REPLICA_ROUTING=0)
  B: async analytical worker -> replica (REPLICA_ROUTING=1)

Matched across configurations:
  - Same tenant + dataset
  - Same interactive workload (50 QPS)
  - Same analytical concurrency (8 jobs)
  - Same measurement window (30 seconds each)
  - Same experimental SLO = 2.0 ms

Measures per configuration:
  Interactive: reqs, P50, P95, P99, max, SLO violation rate
  Analytical:  TPS, exec_ms, queue_wait_ms, tot_lat_ms, routing target distribution,
               lag_bytes, lag_ms, failures, fallbacks
"""

import asyncio
import time
import httpx
import numpy as np
import subprocess
import os
import sys
import random
import json
import psycopg
import uuid
from datetime import datetime

DURATION = 30
INTERACTIVE_QPS = 50
ANALYTICAL_CONCURRENCY = 8
BASE_URL = "http://localhost:8000"
SLO_MS = 2.0

TOKEN = None
TENANT_ID = None
TARGETS = []

# ── Tenant setup ─────────────────────────────────────────────────────────────
async def setup_tenant():
    global TOKEN, TENANT_ID, TARGETS
    TENANT_ID = str(uuid.uuid4())
    org_name = f"Phase3-{TENANT_ID[:8]}"

    async with httpx.AsyncClient(timeout=15.0) as ac:
        r = await ac.post(f"{BASE_URL}/api/auth/register", json={"org_name": org_name})
        r.raise_for_status()
        TOKEN = r.json()["token"]
        TENANT_ID = r.json()["org_id"]

    conn = psycopg.connect("postgresql://postgres:100978@localhost:5432/postgres", autocommit=True)
    cur = conn.cursor()

    print(f"Loading dataset for tenant {TENANT_ID}...")
    cur.execute("""
        INSERT INTO app.security_events
            (org_id, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source, orig_bytes)
        SELECT %s, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source_geo, orig_ip_bytes
        FROM research.ctu_conn_log
        LIMIT 500000
    """, (TENANT_ID,))

    cur.execute("SELECT DISTINCT id_orig_h FROM app.security_events WHERE org_id = %s LIMIT 500", (TENANT_ID,))
    TARGETS = [r[0] for r in cur.fetchall()]
    print(f"  {len(TARGETS)} IPs loaded for interactive traffic.")
    conn.close()

    # Truncate queues
    conn2 = psycopg.connect("postgresql://postgres:100978@localhost:5432/postgres", autocommit=True)
    conn2.cursor().execute("TRUNCATE research.analytical_jobs")
    try: conn2.cursor().execute("TRUNCATE research.routing_decisions")
    except: pass
    conn2.close()

# ── Waiters ───────────────────────────────────────────────────────────────────
async def interactive_worker(latencies, end_time, headers):
    async with httpx.AsyncClient(timeout=30.0) as ac:
        while time.time() < end_time:
            ip = random.choice(TARGETS) if TARGETS else "1.1.1.1"
            st = time.time()
            try:
                r = await ac.get(f"{BASE_URL}/api/investigate?ip={ip}", headers=headers)
                if r.status_code == 200:
                    latencies.append((time.time() - st) * 1000)
            except Exception:
                pass
            await asyncio.sleep(1.0 / INTERACTIVE_QPS)

async def analytical_submitter(metrics, end_time, headers):
    async with httpx.AsyncClient(timeout=120.0, limits=httpx.Limits(max_connections=50)) as ac:
        while time.time() < end_time:
            try:
                r = await ac.post(f"{BASE_URL}/api/analytical/submit?hours=48", headers=headers)
                if r.status_code != 200:
                    await asyncio.sleep(0.5)
                    continue
                job_id = r.json()["job_id"]

                # Poll for completion
                while True:
                    await asyncio.sleep(0.5)
                    if time.time() > end_time + 15:
                        break
                    r2 = await ac.get(f"{BASE_URL}/api/analytical/jobs/{job_id}", headers=headers)
                    if r2.status_code != 200:
                        continue
                    j = r2.json()
                    if j["status"] in ("COMPLETED", "FAILED"):
                        rmeta = j.get("result_metadata") or {}
                        if isinstance(rmeta, str):
                            try: rmeta = json.loads(rmeta)
                            except: rmeta = {}
                        exec_ms = rmeta.get("execution_s", 0) * 1000
                        target  = rmeta.get("target", "unknown")
                        lag_ms  = rmeta.get("lag_ms", -1.0)
                        lag_bytes = rmeta.get("lag_bytes", -1)
                        reason  = rmeta.get("routing_reason", "")

                        c = datetime.fromisoformat(j["created_at"])
                        s = datetime.fromisoformat(j["started_at"]) if j["started_at"] else c
                        comp = datetime.fromisoformat(j["completed_at"])
                        q_wait = (s - c).total_seconds() * 1000
                        tot_lat = (comp - c).total_seconds() * 1000

                        metrics.append({
                            "success": j["status"] == "COMPLETED",
                            "exec_ms": exec_ms,
                            "q_wait": q_wait,
                            "tot_lat": tot_lat,
                            "target": target,
                            "lag_ms": lag_ms,
                            "lag_bytes": lag_bytes,
                            "reason": reason,
                        })
                        break
            except Exception:
                pass

# ── Single scenario runner ────────────────────────────────────────────────────
async def run_scenario(label, replica_routing: bool):
    print(f"\n=== SCENARIO {label}: {'REPLICA ROUTING' if replica_routing else 'PRIMARY ONLY'} ===")
    headers = {"Authorization": f"Bearer {TOKEN}"}

    # Flush queue between scenarios
    conn = psycopg.connect("postgresql://postgres:100978@localhost:5432/postgres", autocommit=True)
    conn.cursor().execute("TRUNCATE research.analytical_jobs")
    conn.close()

    # Start worker as local Python subprocess (Docker daemon not available on this host)
    env = os.environ.copy()
    env["PG_HOST"] = "127.0.0.1"
    env["PG_USER"] = "postgres"
    env["PG_PASSWORD"] = "100978"
    env["PG_DB"] = "postgres"
    env["REPLICA_ROUTING"] = "1" if replica_routing else "0"
    env["WORKER_ID"] = "p3-worker-1"

    worker_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "worker.py")
    proc = subprocess.Popen(
        [sys.executable, "-u", worker_script],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    await asyncio.sleep(2)  # allow worker to start polling

    end_time = time.time() + DURATION
    latencies = []
    ana_metrics = []

    tasks = [asyncio.create_task(interactive_worker(latencies, end_time, headers))]
    for _ in range(ANALYTICAL_CONCURRENCY):
        tasks.append(asyncio.create_task(analytical_submitter(ana_metrics, end_time, headers)))
    await asyncio.gather(*tasks)

    proc.terminate()  # stop local worker process
    try: proc.wait(timeout=5)
    except: proc.kill()

    # Compile
    successes = [m for m in ana_metrics if m["success"]]
    replicas = [m for m in successes if m["target"] == "replica"]
    primaries = [m for m in successes if m["target"] == "primary"]

    res = {
        "reqs": len(latencies),
        "p50": float(np.percentile(latencies, 50)) if latencies else 0,
        "p95": float(np.percentile(latencies, 95)) if latencies else 0,
        "p99": float(np.percentile(latencies, 99)) if latencies else 0,
        "max": float(max(latencies)) if latencies else 0,
        "slo_viol_pct": sum(1 for x in latencies if x > SLO_MS) / max(len(latencies), 1) * 100,
        "ana_completed": len(successes),
        "ana_failed": len(ana_metrics) - len(successes),
        "ana_tps": len(successes) / DURATION,
        "exec_ms": float(np.mean([m["exec_ms"] for m in successes])) if successes else 0,
        "q_wait_ms": float(np.mean([m["q_wait"] for m in successes])) if successes else 0,
        "tot_lat_ms": float(np.mean([m["tot_lat"] for m in successes])) if successes else 0,
        "routed_replica": len(replicas),
        "routed_primary": len(primaries),
        "lag_ms_mean": float(np.mean([m["lag_ms"] for m in successes if m["lag_ms"] >= 0])) if successes else -1,
        "lag_bytes_mean": float(np.mean([m["lag_bytes"] for m in successes if m["lag_bytes"] >= 0])) if successes else -1,
    }
    return res

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await setup_tenant()
    await asyncio.sleep(3)  # allow replication to catch up initial data load

    res_a = await run_scenario("A (primary)", replica_routing=False)
    await asyncio.sleep(5)
    res_b = await run_scenario("B (replica)", replica_routing=True)

    print("\n\n" + "=" * 80)
    print("PHASE 3 EXPERIMENT RESULTS")
    print("=" * 80)

    def row(label, key, fmt="{:.2f} ms"):
        return f"{label:<32} | {fmt.format(res_a.get(key, 0)):<22} | {fmt.format(res_b.get(key, 0))}"

    print(f"{'Metric':<32} | {'A: Worker -> Primary':<22} | {'B: Worker -> Replica'}")
    print("-" * 80)
    print(row("Interactive P50",        "p50"))
    print(row("Interactive P95",        "p95"))
    print(row("Interactive P99",        "p99"))
    print(row("Interactive Max",        "max"))
    print(row("SLO Violation Rate",     "slo_viol_pct", "{:.2f} %"))
    print(row("Analytical TPS",         "ana_tps",      "{:.2f}"))
    print(row("Worker Exec Time",       "exec_ms"))
    print(row("Queue Wait Time",        "q_wait_ms"))
    print(row("Total Job Latency",      "tot_lat_ms"))
    print(row("Mean Replica Lag (ms)",  "lag_ms_mean"))
    print(row("Mean Replica Lag (B)",   "lag_bytes_mean", "{:.0f}"))
    print(f"{'Routed to Replica':<32} | {res_a.get('routed_replica',0):<22} | {res_b.get('routed_replica',0)}")
    print(f"{'Routed to Primary':<32} | {res_a.get('routed_primary',0):<22} | {res_b.get('routed_primary',0)}")
    print(f"{'Failed Jobs':<32} | {res_a.get('ana_failed',0):<22} | {res_b.get('ana_failed',0)}")
    print("=" * 80)

    with open("phase3_metrics.json", "w") as f:
        json.dump({"primary": res_a, "replica": res_b}, f, indent=2)
    print("Saved: phase3_metrics.json")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
