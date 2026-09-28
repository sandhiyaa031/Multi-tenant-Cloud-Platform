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
    org_name = f"Phase2-Exp-{TENANT_ID[:8]}"
    
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r = await ac.post(f"{BASE_URL}/api/auth/register", json={"org_name": org_name})
        r.raise_for_status()
        TOKEN = r.json()["token"]
        TENANT_ID = r.json()["org_id"]
        
        run_id = str(uuid.uuid4())
        await ac.put(f"{BASE_URL}/api/experimental/run_id", json={"run_id": run_id})
        
    print(f"Registered experiment tenant: {TENANT_ID}")
    
    conn = psycopg.connect("postgresql://postgres:100978@localhost/postgres")
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("TRUNCATE table research.analytical_jobs;")
        cur.execute("TRUNCATE table research.pool_scaling_events;")
        print("Cloning 1M rows for tenant...")
        cur.execute("""
            INSERT INTO app.security_events (org_id, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source, orig_bytes)
            SELECT %s, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source_geo, orig_ip_bytes
            FROM research.ctu_conn_log
            LIMIT 1000000
        """, (TENANT_ID,))
        
        cur.execute("SELECT DISTINCT id_orig_h FROM app.security_events WHERE org_id = %s LIMIT 500", (TENANT_ID,))
        for row in cur.fetchall():
            TARGETS.append(row[0])
            
        print(f"Loaded {len(TARGETS)} targets.")
    except Exception as e:
        pass
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
            except Exception:
                pass
            await asyncio.sleep(1.0 / INTERACTIVE_QPS)

async def analytical_sync_worker(results_tps, end_time, headers):
    async with httpx.AsyncClient(timeout=120.0) as ac:
        while time.time() < end_time:
            st = time.time()
            try:
                r = await ac.get(f"{BASE_URL}/api/aggregate?hours=24", headers=headers)
                if r.status_code == 200:
                    results_tps.append((time.time() - st) * 1000)
            except Exception:
                pass

async def analytical_async_worker(metrics, end_time, headers):
    async with httpx.AsyncClient(timeout=120.0, limits=httpx.Limits(max_connections=50, max_keepalive_connections=50)) as ac:
        while time.time() < end_time:
            st_created = time.time()
            try:
                r_sub = await ac.post(f"{BASE_URL}/api/analytical/submit?hours=48", headers=headers)
                if r_sub.status_code != 200:
                    continue
                job_id = r_sub.json()["job_id"]
                
                while True:
                    await asyncio.sleep(0.5)
                    if time.time() > end_time + 10: 
                        break
                    r_check = await ac.get(f"{BASE_URL}/api/analytical/jobs/{job_id}", headers=headers)
                    if r_check.status_code == 200:
                        j = r_check.json()
                        if j["status"] in ["COMPLETED", "FAILED"]:
                            if j["status"] == "COMPLETED" and j.get("result_metadata"):
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
                                "queue_wait": q_wait, "exec_ms": exec_time_ms, "tot_lat": tot_lat,
                                "success": 1 if j["status"] == "COMPLETED" else 0
                            })
                            break
            except Exception:
                pass

async def do_sync_experiment():
    print("=== SCENARIO A: SYNCHRONOUS ===")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    interactive_latencies = []
    analytical_latencies = []
    end_time = time.time() + DURATION
    
    tasks = [asyncio.create_task(interactive_worker(interactive_latencies, end_time, headers))]
    for _ in range(ANALYTICAL_CONCURRENCY):
        tasks.append(asyncio.create_task(analytical_sync_worker(analytical_latencies, end_time, headers)))
        
    await asyncio.gather(*tasks)
    return compile_metrics(interactive_latencies, [], analytical_latencies, DURATION)

def compile_metrics(interactive_latencies, ana_metrics, sync_latencies, dur, extra=None):
    if sync_latencies:
        successes = sync_latencies
        ana_tps = len(successes) / dur
        exec_ms = np.mean(successes) if successes else 0
        q_wait_ms = 0
        tot_lat_ms = exec_ms
    else:
        successes = [m for m in ana_metrics if m["success"] == 1]
        ana_tps = len(successes) / dur
        q_wait_ms = np.mean([m["queue_wait"] for m in successes]) if successes else 0
        exec_ms = np.mean([m["exec_ms"] for m in successes]) if successes else 0
        tot_lat_ms = np.mean([m["tot_lat"] for m in successes]) if successes else 0

    interactive_latencies.sort()
    res = {
        "reqs": len(interactive_latencies),
        "p50": np.percentile(interactive_latencies, 50) if interactive_latencies else 0,
        "p95": np.percentile(interactive_latencies, 95) if interactive_latencies else 0,
        "p99": np.percentile(interactive_latencies, 99) if interactive_latencies else 0,
        "max": max(interactive_latencies) if interactive_latencies else 0,
        "slo_viol_pct": sum(1 for x in interactive_latencies if x > 2.0) / max(len(interactive_latencies), 1) * 100,
        "ana_tps": ana_tps,
        "q_wait_ms": q_wait_ms,
        "exec_ms": exec_ms,
        "tot_lat_ms": tot_lat_ms
    }
    if extra: res.update(extra)
    return res

async def do_async_single_worker():
    print("=== SCENARIO B: ASYNC SINGLE WORKER ===")
    conn = psycopg.connect("postgresql://postgres:100978@localhost/postgres"); conn.autocommit = True
    conn.cursor().execute("TRUNCATE table research.analytical_jobs;")
    conn.close()
    
    subprocess.run(["docker", "rm", "-f"] + [f"dbpilot-worker-{i}" for i in range(1,10)] + ["dbpilot-worker"], capture_output=True)
    proc = subprocess.Popen([
        "docker", "run", "--rm", "--name", "dbpilot-worker", "--cpus=2.0", "--memory=2g",
        "-e", "PG_HOST=host.docker.internal", "-e", "PG_USER=postgres", 
        "-e", "PG_PASSWORD=100978", "-e", "PG_DB=postgres", "dbpilot/worker:phase1"
    ], stdout=subprocess.DEVNULL)
    
    await asyncio.sleep(3)
    end_time = time.time() + DURATION
    headers = {"Authorization": f"Bearer {TOKEN}"}
    interactive_latencies = []
    ana_metrics = []
    
    tasks = [asyncio.create_task(interactive_worker(interactive_latencies, end_time, headers))]
    for _ in range(ANALYTICAL_CONCURRENCY):
        tasks.append(asyncio.create_task(analytical_async_worker(ana_metrics, end_time, headers)))
        
    await asyncio.gather(*tasks)
    subprocess.run(["docker", "rm", "-f", "dbpilot-worker"], capture_output=True)
    return compile_metrics(interactive_latencies, ana_metrics, [], DURATION)


async def poll_elastic_metrics(results, end_time):
    # record min, max, mean workers, scaler actions
    conn = psycopg.connect("postgresql://postgres:100978@localhost/postgres"); conn.autocommit = True
    counts = []
    while time.time() < end_time:
        out = subprocess.run(["docker", "ps", "-q", "-f", "name=dbpilot-worker"], capture_output=True, text=True)
        num = len([x for x in out.stdout.split("\n") if x.strip()])
        counts.append(num)
        await asyncio.sleep(2)
        
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM research.pool_scaling_events WHERE action='SCALE_UP'")
    su = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM research.pool_scaling_events WHERE action='SCALE_DOWN'")
    sd = cur.fetchone()[0]
    conn.close()
    
    results.update({
        "min_workers": min(counts) if counts else 0,
        "max_workers": max(counts) if counts else 0,
        "mean_workers": np.mean(counts) if counts else 0,
        "scale_ups": su,
        "scale_downs": sd
    })


async def do_elastic_experiment():
    print("=== SCENARIO C: ELASTIC WORKER POOL ===")
    conn = psycopg.connect("postgresql://postgres:100978@localhost/postgres"); conn.autocommit = True
    conn.cursor().execute("TRUNCATE table research.analytical_jobs;")
    try: conn.cursor().execute("TRUNCATE table research.pool_scaling_events;")
    except: pass
    conn.close()
    
    subprocess.run(["docker", "rm", "-f"] + [f"dbpilot-worker-{i}" for i in range(1,10)], capture_output=True)
    
    pool_proc = subprocess.Popen(["python", "backend/pool_controller.py"])
    await asyncio.sleep(5) # wait for controller to boot and spawn MIN_WORKERS
    
    end_time = time.time() + DURATION
    headers = {"Authorization": f"Bearer {TOKEN}"}
    interactive_latencies = []
    ana_metrics = []
    elastic_metrics = {}
    
    tasks = [
        asyncio.create_task(interactive_worker(interactive_latencies, end_time, headers)),
        asyncio.create_task(poll_elastic_metrics(elastic_metrics, end_time))
    ]
    for _ in range(ANALYTICAL_CONCURRENCY):
        tasks.append(asyncio.create_task(analytical_async_worker(ana_metrics, end_time, headers)))
        
    await asyncio.gather(*tasks)
    
    pool_proc.terminate()
    try: pool_proc.wait(timeout=3)
    except: pool_proc.kill()
    subprocess.run(["docker", "rm", "-f"] + [f"dbpilot-worker-{i}" for i in range(1,10)], capture_output=True)
    
    return compile_metrics(interactive_latencies, ana_metrics, [], DURATION, elastic_metrics)

async def main():
    await setup_test_tenant()
    
    sync_res = await do_sync_experiment()
    await asyncio.sleep(5)
    async_res = await do_async_single_worker()
    await asyncio.sleep(5)
    elastic_res = await do_elastic_experiment()
    
    print("\n\n" + "="*80)
    print("PHASE 2 ELASTICITY EXPERIMENT RESULTS")
    print("="*80)
    
    def p(label, key, fmt="{:.3f} ms"):
        return f"{label:<25} | {fmt.format(sync_res.get(key,0)):<20} | {fmt.format(async_res.get(key,0)):<20} | {fmt.format(elastic_res.get(key,0)):<20}"
        
    print(f"{'Metric':<25} | {'A: Sync (A=8)':<20} | {'B: Async (Static=1)':<20} | {'C: Elastic (1-8)':<20}")
    print("-" * 90)
    print(p("Interactive P50", "p50"))
    print(p("Interactive P95", "p95"))
    print(p("Interactive P99", "p99"))
    print(p("Interactive Max", "max"))
    print(p("SLO Violations", "slo_viol_pct", "{:.2f} %"))
    print(p("Analytical TPS", "ana_tps", "{:.2f}"))
    print(p("Worker Exec Time", "exec_ms", "{:.1f} ms"))
    print(p("Queue Wait Time", "q_wait_ms", "{:.1f} ms"))
    print(p("Total Job Latency", "tot_lat_ms", "{:.1f} ms"))
    
    print("-" * 90)
    print(f"Elastic Min Workers: {elastic_res.get('min_workers')} | Max: {elastic_res.get('max_workers')} | Mean: {elastic_res.get('mean_workers',0):.1f}")
    print(f"Total Scale-Ups: {elastic_res.get('scale_ups')} | Total Scale-Downs: {elastic_res.get('scale_downs')}")
    print("="*80)
    
    with open('phase2_metrics.json', 'w') as f:
        json.dump({'sync': sync_res, 'async_single': async_res, 'elastic': elastic_res}, f)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
