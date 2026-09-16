import asyncio
import os
import time
import random
import json
import csv
from pathlib import Path
from dotenv import load_dotenv
import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "backend" / ".env")

PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres")
PG_USER     = os.getenv("PG_USER", "postgres")
PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = os.getenv("PG_PORT", "5432")
PG_DB       = os.getenv("PG_DB", "postgres")
CONN_STR    = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"

# Global State
IP_POOL = []
RESULTS_DIR = PROJECT_ROOT / "results" / "b0"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Metrics Arrays ──
raw_interactive = []  # To store (concurrency, ts, latency, success)
raw_analytical = []   # To store (concurrency, ts, latency, success)
sys_metrics = []

async def init_ip_pool():
    print("Loading pre-calculated IP Pool for interactive queries...")
    pool_file = PROJECT_ROOT / "backend" / "scripts" / "interactive_targets.json"
    global IP_POOL
    with open(pool_file, 'r') as f:
        IP_POOL = json.load(f)
    print(f"Loaded {len(IP_POOL)} guaranteed targets.")

async def collect_sys_metrics(conn, state_label, concurrency):
    async with conn.cursor() as cur:
        # pg_stat_database: blocks read, hit
        await cur.execute(f"SELECT blks_read, blks_hit, xact_commit FROM pg_stat_database WHERE datname='{PG_DB}'")
        row = await cur.fetchone()
        sys_metrics.append({
            "concurrency": concurrency,
            "state": state_label,
            "ts": time.time(),
            "blks_read": row[0],
            "blks_hit": row[1],
            "xact_commit": row[2]
        })

async def run_interactive_query(conn, concurrency):
    target = random.choice(IP_POOL)
    target_ip = target['ip']
    target_date = target['dt']
    
    start_t = time.time()
    success = True
    try:
        # Indexed Lookup: (id_orig_h, ts)
        # Using a fixed bounding box based on the known date of the activity.
        query = f"SELECT uid, conn_state FROM research.ctu_conn_log WHERE id_orig_h = '{target_ip}' AND ts >= '{target_date} 00:00:00' AND ts <= '{target_date} 23:59:59'"
        async with conn.cursor() as cur:
            await cur.execute(query)
            await cur.fetchall()
    except Exception as e:
        success = False
    finally:
        lat = time.time() - start_t
        raw_interactive.append((concurrency, start_t, lat, success))

async def interactive_worker(conns, qps, duration, concurrency):
    start_time = time.time()
    interval = 1.0 / qps
    idx = 0
    num_conns = len(conns)
    tasks = []
    
    while time.time() - start_time < duration:
        loop_start = time.time()
        
        # Round robin on interactive connections
        conn = conns[idx % num_conns]
        idx += 1
        
        # Fire and forget (don't block spawner)
        t = asyncio.create_task(run_interactive_query(conn, concurrency))
        tasks.append(t)
        
        elapsed = time.time() - loop_start
        sleep_time = max(0, interval - elapsed)
        await asyncio.sleep(sleep_time)
        
    await asyncio.gather(*tasks)

async def analytical_worker(conn, duration, concurrency):
    start_time = time.time()
    while time.time() - start_time < duration:
        query_start = time.time()
        success = True
        try:
            # Heavy sequence scan + aggregate across all geos/dates
            query = "SELECT source_geo, sum(orig_ip_bytes), count(*) FROM research.ctu_conn_log GROUP BY source_geo ORDER BY count DESC"
            async with conn.cursor() as cur:
                await cur.execute(query)
                await cur.fetchall()
        except Exception as e:
            success = False
        finally:
            lat = time.time() - query_start
            raw_analytical.append((concurrency, query_start, lat, success))

async def run_experiment_level(A, qps=50, duration=15, warmup=5):
    print(f"\\n--- Running Concurrency Level A={A} ---")
    
    # Establish connections
    ana_conns = [await psycopg.AsyncConnection.connect(CONN_STR) for _ in range(A)]
    # Use 5 persistent connections for the interactive spawner to prevent connection thrashing
    num_int_conns = max(5, int(qps/10))
    int_conns = [await psycopg.AsyncConnection.connect(CONN_STR) for _ in range(num_int_conns)]
    
    # Sys metric conn
    sys_conn = await psycopg.AsyncConnection.connect(CONN_STR)
    
    # Pre-run metrics
    await collect_sys_metrics(sys_conn, "START", A)
    
    print(f"[{A}] Warmup ({warmup}s)...")
    await asyncio.sleep(warmup)
    
    print(f"[{A}] Measurement ({duration}s)...")
    start_mark = len(raw_interactive)
    
    tasks = []
    # Launch analytical workers
    for c in ana_conns:
        tasks.append(asyncio.create_task(analytical_worker(c, duration, A)))
        
    # Launch interactive spawner
    tasks.append(asyncio.create_task(interactive_worker(int_conns, qps, duration, A)))
    
    await asyncio.gather(*tasks)
    
    # Post-run metrics
    await collect_sys_metrics(sys_conn, "END", A)
    
    # Summarize just to terminal
    latencies = [x[2] for x in raw_interactive[start_mark:] if x[3]]
    if latencies:
        latencies.sort()
        p50 = latencies[int(len(latencies)*0.5)]
        p95 = latencies[int(len(latencies)*0.95)]
        p99 = latencies[int(len(latencies)*0.99)]
        print(f"[{A}] Interactive Req: {len(latencies)} | p50: {p50*1000:.1f}ms, p95: {p95*1000:.1f}ms, p99: {p99*1000:.1f}ms")
    else:
        print(f"[{A}] Interactive Req: 0")
        
    # Cleanup
    for c in ana_conns + int_conns + [sys_conn]:
        await c.close()
    
    # Cool down before next level
    await asyncio.sleep(2)

async def main(pilot=False):
    await init_ip_pool()
    random.seed(42) # Deterministic workload sampling
    
    levels = [0, 1, 2, 4, 8] if pilot else [0, 4, 8, 16, 24, 32]
    duration = 10 if pilot else 30
    warmup = 3 if pilot else 10
    QPS = 50 # Fixed interactive load
    
    for A in levels:
        await run_experiment_level(A, qps=QPS, duration=duration, warmup=warmup)

    print("\\n--- Saving CSV Results ---")
    
    # Save interactive raw
    with open(RESULTS_DIR / "raw_request_latencies.csv", 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(["concurrency", "timestamp", "latency_s", "success"])
        w.writerows(raw_interactive)
        
    # Save analytical raw
    with open(RESULTS_DIR / "analytical_results.csv", 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(["concurrency", "timestamp", "latency_s", "success"])
        w.writerows(raw_analytical)
        
    # Save sys metrics
    with open(RESULTS_DIR / "system_metrics.csv", 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=["concurrency", "state", "ts", "blks_read", "blks_hit", "xact_commit"])
        w.writeheader()
        w.writerows(sys_metrics)

    # Compute and save Summary
    with open(RESULTS_DIR / "summary.csv", 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(["concurrency", "int_count", "int_p50_ms", "int_p95_ms", "int_p99_ms", "int_max_ms", "ana_count", "ana_tps"])
        
        for A in levels:
            int_lats = [x[2] for x in raw_interactive if x[0] == A and x[3]]
            ana_lats = [x[2] for x in raw_analytical if x[0] == A and x[3]]
            
            p50 = p95 = p99 = vmax = 0
            if int_lats:
                int_lats.sort()
                p50 = int_lats[int(len(int_lats)*0.5)] * 1000
                p95 = int_lats[int(len(int_lats)*0.95)] * 1000
                p99 = int_lats[int(len(int_lats)*0.99)] * 1000
                vmax = int_lats[-1] * 1000
                
            ana_cnt = len(ana_lats)
            ana_tps = ana_cnt / duration
            
            w.writerow([A, len(int_lats), p50, p95, p99, vmax, ana_cnt, ana_tps])

    print("Experiment fully finished. Results stored in results/b0/")

if __name__ == '__main__':
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    is_pilot = "--pilot" in sys.argv
    asyncio.run(main(pilot=is_pilot))
