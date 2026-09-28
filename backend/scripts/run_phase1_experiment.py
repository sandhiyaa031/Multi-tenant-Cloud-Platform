import asyncio
import time
import httpx
import numpy as np
import subprocess
import os
import sys
import random
import json
import psutil
from collections import deque
from datetime import datetime

# Common Configuration
DURATION = 30
INTERACTIVE_QPS = 50
ANALYTICAL_CONCURRENCY = 8
TARGETS = []
BASE_URL = "http://localhost:8000"
TOKEN = None
TENANT_ID = "00000000-0000-0000-0000-000000000000"

async def setup_test_tenant():
    global TOKEN, TENANT_ID, TARGETS
    import uuid
    TENANT_ID = str(uuid.uuid4())
    org_name = f"Phase1-Exp-{TENANT_ID[:8]}"
    
    # 1. Register tenant
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r = await ac.post(f"{BASE_URL}/api/auth/register", json={"org_name": org_name})
        r.raise_for_status()
        TOKEN = r.json()["token"]
        TENANT_ID = r.json()["org_id"]
        
        # 2. Set an active test run_id to make AdmissionController metrics persistence work
        run_id = str(uuid.uuid4())
        await ac.put(f"{BASE_URL}/api/experimental/run_id", json={"run_id": run_id})
        
    print(f"Registered generic experiment tenant: {TENANT_ID} configured active run_id: {run_id}")
    
    # We must seed data into app.security_events for this tenant, exactly mirroring the original dataset size? 
    # WAIT! The original Phase 0 tests used a Global schema / fixed table for B0 baseline on 4.9M rows.
    # But RLS strict implementation demands querying the tenant's exact data in app.security_events.
    # For a fair comparison to Phase 0, we can quickly map 1 million rows into this tenant. 
    # Realistically, we can just run the experiment against a heavy query directly via python psycopg to assign tenant_id en-masse.
    import psycopg
    conn = psycopg.connect("postgresql://postgres:100978@localhost/postgres")
    conn.autocommit = True
    try:
        cur = conn.cursor()
        print("Cloning 1M rows into tenant context to simulate realistic scale (this may take a minute...)")
        cur.execute("""
            INSERT INTO app.security_events (org_id, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source, orig_bytes)
            SELECT %s, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source_geo, orig_ip_bytes
            FROM research.ctu_conn_log
            LIMIT 1000000
        """, (TENANT_ID,))
        
        # Load targets
        cur.execute("SELECT DISTINCT id_orig_h FROM app.security_events WHERE org_id = %s LIMIT 500", (TENANT_ID,))
        for row in cur.fetchall():
            TARGETS.append(row[0])
            
        print(f"Cloned subset for tenant {TENANT_ID}. Loaded {len(TARGETS)} targets.")
    except Exception as e:
        print("Cloning skipped (likely already exists) or failed:", e)
    finally:
        conn.close()

async def interactive_worker(results, end_time, headers):
    async with httpx.AsyncClient(timeout=30.0) as ac:
        while time.time() < end_time:
            ip = random.choice(TARGETS) if TARGETS else "1.1.1.1"
            st = time.time()
            try:
                r = await ac.get(f"{BASE_URL}/api/investigate?ip={ip}", headers=headers)
                lat = (time.time() - st) * 1000
                if r.status_code == 200:
                    results.append(lat)
                else:
                    print(f"Interactive Error {r.status_code}: {r.text}")
            except Exception as e:
                print(f"Interactive Exception: {e}")
            
            await asyncio.sleep(1.0 / INTERACTIVE_QPS)

async def analytical_sync_worker(results_tps, end_time, headers):
    async with httpx.AsyncClient(timeout=120.0) as ac:
        while time.time() < end_time:
            st = time.time()
            try:
                # Same heavy query logic hit synchronously
                r = await ac.get(f"{BASE_URL}/api/aggregate?hours=24", headers=headers)
                if r.status_code == 200:
                    results_tps.append((time.time() - st) * 1000)
                else:
                    print(f"Sync Analytical Error {r.status_code}: {r.text}")
            except Exception as e:
                print(f"Sync Analytical Exception: {e}")

async def do_sync_experiment():
    print("=== SYNCHRONOUS EXPERIMENT (A=8) ===")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    interactive_latencies = []
    analytical_latencies = []
    end_time = time.time() + DURATION
    
    tasks = [asyncio.create_task(interactive_worker(interactive_latencies, end_time, headers))]
    for _ in range(ANALYTICAL_CONCURRENCY):
        tasks.append(asyncio.create_task(analytical_sync_worker(analytical_latencies, end_time, headers)))
        
    await asyncio.gather(*tasks)
    
    return {
        "p50": np.percentile(interactive_latencies, 50) if interactive_latencies else 0,
        "p95": np.percentile(interactive_latencies, 95) if interactive_latencies else 0,
        "p99": np.percentile(interactive_latencies, 99) if interactive_latencies else 0,
        "slo_viol": sum(1 for x in interactive_latencies if x > 2.0) / max(len(interactive_latencies), 1) * 100,
        "ana_tps": len(analytical_latencies) / DURATION,
        "ana_exec_ms": np.mean(analytical_latencies) if analytical_latencies else 0
    }

async def analytical_async_worker(metrics, end_time, headers):
    async with httpx.AsyncClient(timeout=120.0) as ac:
        while time.time() < end_time:
            st_created = time.time()
            try:
                # Submit job
                r_sub = await ac.post(f"{BASE_URL}/api/analytical/submit?hours=48", headers=headers)
                if r_sub.status_code != 200:
                    print(f"Async Submit Error {r_sub.status_code}: {r_sub.text}")
                    continue
                job_id = r_sub.json()["job_id"]
                
                # Poll until complete
                while True:
                    await asyncio.sleep(0.5)
                    if time.time() > end_time + 10: # allow grace
                        break
                    r_check = await ac.get(f"{BASE_URL}/api/analytical/jobs/{job_id}", headers=headers)
                    if r_check.status_code == 200:
                        j = r_check.json()
                        if j["status"] in ["COMPLETED", "FAILED"]:
                            if j["status"] == "COMPLETED" and "result_metadata" in j and j["result_metadata"]:
                                rmeta = j["result_metadata"]
                                if isinstance(rmeta, str): rmeta = json.loads(rmeta)
                                exec_time_ms = rmeta.get("execution_s", 0) * 1000
                            else:
                                exec_time_ms = 0
                            
                            c = datetime.fromisoformat(j["created_at"])
                            s = datetime.fromisoformat(j["started_at"]) if j["started_at"] else c
                            comp = datetime.fromisoformat(j["completed_at"])
                            q_wait = (s - c).total_seconds() * 1000
                            tot_lat = (comp - c).total_seconds() * 1000
                            
                            metrics.append({
                                "queue_wait": q_wait,
                                "exec_ms": exec_time_ms,
                                "tot_lat": tot_lat,
                                "success": 1 if j["status"] == "COMPLETED" else 0
                            })
                            break
                    else:
                        print(f"Async Poll Error {r_check.status_code}: {r_check.text}")
            except Exception as e:
                print(f"Async Exception: {e}")


async def monitor_docker_stats(docker_metrics, end_time):
    while time.time() < end_time:
        try:
            res = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", '{"cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}"}', "dbpilot-worker"],
                capture_output=True, text=True
            )
            if res.returncode == 0 and res.stdout.strip():
                try:
                    js = json.loads(res.stdout.strip().split("\n")[0])
                    cpu = float(js["cpu"].replace("%",""))
                    docker_metrics.append(cpu)
                except: pass
        except: pass
        await asyncio.sleep(2.0)

async def do_async_experiment():
    print("=== ASYNCHRONOUS EXPERIMENT (A=8) ===")
    
    # 1. Boot Container
    subprocess.run(["docker", "rm", "-f", "dbpilot-worker"], capture_output=True)
    print("Booting Worker Container...")
    proc = subprocess.Popen([
        "docker", "run", "--rm", "--name", "dbpilot-worker", 
        "--cpus=2.0", "--memory=2g",
        "-e", "PG_HOST=host.docker.internal",
        "-e", "PG_USER=postgres",
        "-e", "PG_PASSWORD=100978",
        "-e", "PG_DB=postgres",
        "dbpilot/worker:phase1"
    ], stdout=open('docker.log', 'w'), stderr=subprocess.STDOUT)
    
    await asyncio.sleep(3) # wait for boot
    end_time = time.time() + DURATION
    
    headers = {"Authorization": f"Bearer {TOKEN}"}
    interactive_latencies = []
    ana_metrics = []
    docker_metrics = []
    
    tasks = [
        asyncio.create_task(interactive_worker(interactive_latencies, end_time, headers)),
        asyncio.create_task(monitor_docker_stats(docker_metrics, end_time))
    ]
    for _ in range(ANALYTICAL_CONCURRENCY):
        tasks.append(asyncio.create_task(analytical_async_worker(ana_metrics, end_time, headers)))
        
    await asyncio.gather(*tasks)
    
    # Kill container
    subprocess.run(["docker", "rm", "-f", "dbpilot-worker"], capture_output=True)
    
    successes = [m for m in ana_metrics if m["success"] == 1]
    
    return {
        "p50": np.percentile(interactive_latencies, 50) if interactive_latencies else 0,
        "p95": np.percentile(interactive_latencies, 95) if interactive_latencies else 0,
        "p99": np.percentile(interactive_latencies, 99) if interactive_latencies else 0,
        "slo_viol": sum(1 for x in interactive_latencies if x > 2.0) / max(len(interactive_latencies), 1) * 100,
        "ana_tps": len(successes) / DURATION,
        "q_wait_ms": np.mean([m["queue_wait"] for m in successes]) if successes else 0,
        "exec_ms": np.mean([m["exec_ms"] for m in successes]) if successes else 0,
        "tot_lat_ms": np.mean([m["tot_lat"] for m in successes]) if successes else 0,
        "docker_cpu_pct": np.mean(docker_metrics) if docker_metrics else 0
    }

async def main():
    await setup_test_tenant()
    
    try:
        sync_res = await do_sync_experiment()
        await asyncio.sleep(5)
        async_res = await do_async_experiment()
        
        print("\n\n" + "="*80)
        print("PHASE 1 EXPERIMENT RESULTS")
        print("="*80)
        
        print(f"{'Metric':<25} | {'Synchronous (API)':<20} | {'Async (Docker)':<20}")
        print("-" * 75)
        print(f"{'Interactive P50':<25} | {sync_res['p50']:<17.3f} ms | {async_res['p50']:<17.3f} ms")
        print(f"{'Interactive P95':<25} | {sync_res['p95']:<17.3f} ms | {async_res['p95']:<17.3f} ms")
        print(f"{'Interactive P99':<25} | {sync_res['p99']:<17.3f} ms | {async_res['p99']:<17.3f} ms")
        print(f"{'SLO Violation Rate':<25} | {sync_res['slo_viol']:<17.2f} %  | {async_res['slo_viol']:<17.2f} %")
        print(f"{'Analytical TPS':<25} | {sync_res['ana_tps']:<20.2f} | {async_res['ana_tps']:<20.2f}")
        print(f"{'Worker Execution Time':<25} | {sync_res['ana_exec_ms']:<17.1f} ms | {async_res['exec_ms']:<17.1f} ms")
        print(f"{'Queue Wait Time':<25} | N/A                  | {async_res['q_wait_ms']:<17.1f} ms")
        print(f"{'Total Job Latency':<25} | {sync_res['ana_exec_ms']:<17.1f} ms | {async_res['tot_lat_ms']:<17.1f} ms")
        print(f"{'Worker CPU Usage':<25} | N/A                  | {async_res['docker_cpu_pct']:<17.1f} %")
        print("="*80)
        
        with open('phase1_metrics.json', 'w') as f:
            json.dump({'sync': sync_res, 'async': async_res}, f)
            
    except Exception as e:
        print("Experiment failed:", e)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
