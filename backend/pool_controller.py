import asyncio
import os
import subprocess
import time
from datetime import datetime
import psycopg

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.db import CONN_INFO

# Bounded Elasticity Limits
MIN_WORKERS = 1
MAX_WORKERS = 8

# thresholds
SCALE_UP_THRESHOLD = 2    # More than 2 PENDING jobs triggers scale up
SCALE_DOWN_THRESHOLD = 0  # 0 PENDING jobs triggers scale down

# Hysteresis / Cooldown (seconds)
COOLDOWN_SEC = 3.0

class ElasticController:
    def __init__(self):
        self.active_workers = set()
        self.last_scale_time = 0.0

    async def init_db(self):
        # Create scaling events table
        async with await psycopg.AsyncConnection.connect(CONN_INFO) as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS research.pool_scaling_events (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP DEFAULT NOW(),
                    previous_worker_count INT,
                    new_worker_count INT,
                    queue_depth INT,
                    reason TEXT,
                    action TEXT
                )
            ''')
            # Clear previous workers
            subprocess.run(["docker", "rm", "-f"] + [f"dbpilot-worker-{i}" for i in range(1, 10)], capture_output=True)

    async def measure_queue(self):
        async with await psycopg.AsyncConnection.connect(CONN_INFO) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM research.analytical_jobs WHERE status = 'PENDING'")
                return (await cur.fetchone())[0]

    async def log_event(self, old_c, new_c, q, reason, action):
        async with await psycopg.AsyncConnection.connect(CONN_INFO) as conn:
            await conn.execute('''
                INSERT INTO research.pool_scaling_events 
                (previous_worker_count, new_worker_count, queue_depth, reason, action) 
                VALUES (%s, %s, %s, %s, %s)
            ''', (old_c, new_c, q, reason, action))

    async def spawn_worker(self):
        wid = len(self.active_workers) + 1
        name = f"dbpilot-worker-{wid}"
        self.active_workers.add(name)
        subprocess.Popen([
            "docker", "run", "--rm", "--name", name, 
            "--cpus=2.0", "--memory=2g",
            "-e", "PG_HOST=host.docker.internal",
            "-e", "PG_USER=postgres",
            "-e", "PG_PASSWORD=100978",
            "-e", "PG_DB=postgres",
            "-e", f"WORKER_ID={name}",
            "dbpilot/worker:phase1"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return name

    async def retire_worker(self):
        if not self.active_workers: return None
        # pop the largest number
        wid = len(self.active_workers)
        name = f"dbpilot-worker-{wid}"
        if name in self.active_workers:
            self.active_workers.remove(name)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            return name
        return None
        
    async def loop(self):
        await self.init_db()
        print("Elastic Pool Controller Started.")
        
        # Enforce minimum workers immediately
        while len(self.active_workers) < MIN_WORKERS:
            await self.spawn_worker()
            
        try:
            while True:
                q = await self.measure_queue()
                c = len(self.active_workers)
                
                # Check cooldown
                if time.time() - self.last_scale_time >= COOLDOWN_SEC:
                    if q > SCALE_UP_THRESHOLD and c < MAX_WORKERS:
                        await self.spawn_worker()
                        new_c = len(self.active_workers)
                        await self.log_event(c, new_c, q, f"Queue depth {q} > {SCALE_UP_THRESHOLD}", "SCALE_UP")
                        print(f"SCALE UP [{c}->{new_c}] (Queue: {q})")
                        self.last_scale_time = time.time()
                        
                    elif q <= SCALE_DOWN_THRESHOLD and c > MIN_WORKERS:
                        await self.retire_worker()
                        new_c = len(self.active_workers)
                        await self.log_event(c, new_c, q, f"Queue depth {q} <= {SCALE_DOWN_THRESHOLD}", "SCALE_DOWN")
                        print(f"SCALE DOWN [{c}->{new_c}] (Queue: {q})")
                        self.last_scale_time = time.time()
                
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Controller Error: {e}")
        finally:
            print("Cleaning up workers...")
            for w in list(self.active_workers):
                subprocess.run(["docker", "rm", "-f", w], capture_output=True)
                
if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    controller = ElasticController()
    try:
        asyncio.run(controller.loop())
    except KeyboardInterrupt:
        pass
