import asyncio
import psycopg
import time
import json
import random
import os
import csv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_URL = "postgresql://postgres:100978@localhost:5432/postgres"

IP_POOL = []
SLO_MS = 2.0
SLO_S = SLO_MS / 1000.0

# B0 configuration
B0_CONCURRENCY = 32

# B1 configuration
B1_CONCURRENCY = 8

# Measurement Window
EXPERIMENT_DURATION = 30
WARMUP_DURATION = 10

class MiddlewareController:
    def __init__(self, mode, target_concurrency):
        self.mode = mode # 'b0', 'b1', 'b2'
        self.target_concurrency = target_concurrency
        self.running_analytical = 0
        self.interactive_latencies = []
        self.completed_analytical = 0
        self.is_running = True
        self.slo_violations_history = []
        self.target_history = []
        
    def adjust_b2(self):
        if self.mode != 'b2':
            return
            
        if not self.interactive_latencies:
            return
            
        recent = self.interactive_latencies[-100:] # last 100 reqs
        violations = sum(1 for lat in recent if lat > SLO_S)
        viol_rate = violations / len(recent)
        self.slo_violations_history.append((time.time(), viol_rate * 100))
        self.target_history.append((time.time(), self.target_concurrency))
        
        # Deadband controller
        if viol_rate > 0.015: # > 1.5% violations -> scale down
            self.target_concurrency = max(1, self.target_concurrency - 2)
        elif viol_rate < 0.005: # < 0.5% violations -> scale up
            self.target_concurrency = min(32, self.target_concurrency + 1)
            
    async def adaptive_loop(self):
        while self.is_running:
            await asyncio.sleep(2.0)
            self.adjust_b2()

async def interactive_worker(controller, qps):
    interval = 1.0 / qps
    async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
        while controller.is_running:
            target = random.choice(IP_POOL)
            start_t = time.time()
            try:
                query = f"SELECT uid, conn_state FROM research.ctu_conn_log WHERE id_orig_h = '{target['ip']}' AND ts >= '{target['dt']} 00:00:00' AND ts <= '{target['dt']} 23:59:59'"
                async with conn.cursor() as cur:
                    await cur.execute(query)
                    await cur.fetchall()
            except Exception:
                pass
            
            lat = time.time() - start_t
            controller.interactive_latencies.append(lat)
            
            # Rate limiting
            elapsed = time.time() - start_t
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)

async def analytical_worker(controller):
    async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
        while controller.is_running:
            if controller.running_analytical >= controller.target_concurrency:
                await asyncio.sleep(0.1)
                continue
                
            controller.running_analytical += 1
            try:
                query = "SELECT source_geo, sum(orig_ip_bytes), count(*) FROM research.ctu_conn_log GROUP BY source_geo ORDER BY count DESC"
                async with conn.cursor() as cur:
                    await cur.execute(query)
                    await cur.fetchall()
                controller.completed_analytical += 1
            except Exception:
                pass
            finally:
                controller.running_analytical -= 1

async def run_experiment(mode):
    print(f"\\n============================")
    print(f"STARTING {mode.upper()} EXPERIMENT")
    print(f"============================")
    
    # Setup controller
    initial_target = B0_CONCURRENCY if mode == 'b0' else B1_CONCURRENCY
    controller = MiddlewareController(mode, initial_target)
    
    # Start tasks
    tasks = []
    
    # 1 Interactive worker generating 50 QPS
    tasks.append(asyncio.create_task(interactive_worker(controller, qps=50)))
    
    # 32 potential analytical workers acting as the unbounded queue
    for _ in range(32):
        tasks.append(asyncio.create_task(analytical_worker(controller)))
        
    if mode == 'b2':
        tasks.append(asyncio.create_task(controller.adaptive_loop()))
        
    print(f"Warmup ({WARMUP_DURATION}s)...")
    await asyncio.sleep(WARMUP_DURATION)
    
    # Clear warmup metrics
    controller.interactive_latencies.clear()
    controller.completed_analytical = 0
    controller.slo_violations_history.clear()
    controller.target_history.clear()
    start_time = time.time()
    
    print(f"Measurement ({EXPERIMENT_DURATION}s)...")
    await asyncio.sleep(EXPERIMENT_DURATION)
    
    controller.is_running = False
    
    print(f"Stopping workers...")
    for t in tasks:
        t.cancel()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # Calculate metrics
    lats = sorted(controller.interactive_latencies)
    if not lats:
        print("No interactive latencies captured?!")
        return None
        
    p50 = lats[int(len(lats)*0.50)] * 1000
    p95 = lats[int(len(lats)*0.95)] * 1000
    p99 = lats[int(len(lats)*0.99)] * 1000
    pmax = lats[-1] * 1000
    
    viol = sum(1 for l in lats if l > SLO_S)
    viol_rate = (viol / len(lats)) * 100
    
    atps = controller.completed_analytical / EXPERIMENT_DURATION
    
    print(f"\\n--- {mode.upper()} FINISHED ---")
    print(f"Int requests: {len(lats)}")
    print(f"P50: {p50:.2f}ms | P95: {p95:.2f}ms | P99: {p99:.2f}ms | Max: {pmax:.2f}ms")
    print(f"SLO Violations (> {SLO_MS}ms): {viol_rate:.2f}%")
    print(f"Analytical Completed: {controller.completed_analytical}")
    print(f"Analytical TPS: {atps:.2f}")
    
    # Dump raw traces
    out_dir = PROJECT_ROOT / "results" / "final" / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    
    with open(out_dir / "interactive.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["latency_ms"])
        for l in lats:
            writer.writerow([l*1000])
            
    if mode == 'b2':
        with open(out_dir / "b2_trace.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "violation_rate", "target_concurrency"])
            # Interpolate to match sizes roughly
            for i in range(len(controller.target_history)):
                t, targ = controller.target_history[i]
                v = controller.slo_violations_history[i][1] if i < len(controller.slo_violations_history) else 0.0
                writer.writerow([t, v, targ])
                
    return {
        "mode": mode,
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "viol_rate": viol_rate,
        "atps": atps
    }

async def main():
    print("Loading pre-calculated IP Pool for interactive queries...")
    with open(PROJECT_ROOT / "backend" / "scripts" / "interactive_targets.json", 'r') as f:
        global IP_POOL
        IP_POOL = json.load(f)
    print(f"Loaded {len(IP_POOL)} guaranteed targets.")
    
    random.seed(42)
    
    results = []
    results.append(await run_experiment('b0'))
    results.append(await run_experiment('b1'))
    results.append(await run_experiment('b2'))
    
    # Save final comparison
    out_dir = PROJECT_ROOT / "results" / "final"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "comparison.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["mode", "p50", "p95", "p99", "slo_viol_rate", "ana_tps"])
        for r in results:
            writer.writerow([r["mode"], r["p50"], r["p95"], r["p99"], r["viol_rate"], r["atps"]])
            
    print("\\n==================================")
    print("FINAL EXPERIMENTAL SUMMARY")
    print("==================================")
    print(" Mode | P99 (ms) | SLO Viol (%) | Ana TPS")
    for r in results:
        print(f" {r['mode'].upper():<4} | {r['p99']:8.2f} | {r['viol_rate']:12.2f} | {r['atps']:7.2f}")
    print("==================================")

if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
