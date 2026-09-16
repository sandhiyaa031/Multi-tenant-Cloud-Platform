import asyncio
import psycopg
import time
import sys
import json
import os

DB_URL = "postgresql://postgres:100978@localhost:5432/postgres"
WORKER_ID = f"worker-{os.getpid()}"

async def process_job(job_id: str, tenant_id: str, workload: str):
    print(f"[{WORKER_ID}] Claimed Job {job_id} for Tenant {tenant_id}")
    
    try:
        # Isolated execution: We simulate a heavily isolated memory footprint here.
        async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
            async with conn.cursor() as cur:
                # 1. Enforce Tenant Context via RLS
                await cur.execute("SELECT set_config('request.jwt.claim.tenant_id', %s, false)", (str(tenant_id),))
                
                # 2. Execution Phase
                print(f"[{WORKER_ID}] Executing heavy analytical workload for {tenant_id}...")
                start_t = time.time()
                await cur.execute('''
                    SELECT source_geo, proto, count(*) as flow_count, sum(orig_bytes) as total_bytes
                    FROM app.security_events
                    GROUP BY source_geo, proto
                    ORDER BY flow_count DESC
                ''')
                rows = await cur.fetchall()
                elapsed = time.time() - start_t
                
                # 3. Save Result Payload to Job
                payload = {
                    "worker_id": WORKER_ID,
                    "results_count": len(rows),
                    "execution_s": round(elapsed, 4)
                }
                
                await cur.execute('''
                    UPDATE research.analytical_jobs 
                    SET status = 'COMPLETED', 
                        completed_at = NOW(),
                        result_metadata = %s
                    WHERE job_id = %s
                ''', (json.dumps(payload), job_id))
                await conn.commit()
                
                print(f"[{WORKER_ID}] Job {job_id} Completed in {elapsed:.2f}s.")
                
    except Exception as e:
        print(f"[{WORKER_ID}] Job {job_id} FAILED: {str(e)}")
        async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT set_config('request.jwt.claim.tenant_id', %s, false)", (str(tenant_id),))
                await cur.execute('''
                    UPDATE research.analytical_jobs 
                    SET status = 'FAILED', 
                        completed_at = NOW(),
                        error_info = %s
                    WHERE job_id = %s
                ''', (str(e), job_id))
                await conn.commit()


async def worker_loop():
    print(f"[{WORKER_ID}] Elastic Analytical Compute Worker Booted.")
    print(f"[{WORKER_ID}] Polling for PENDING jobs...")
    
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
                async with conn.cursor() as cur:
                    # Polling logic. In a real system you'd use LISTEN/NOTIFY or SKIP LOCKED.
                    # Using a safe polling query. We do NOT apply RLS here initially because 
                    # the worker acts as the system scheduler identifying any tenant's job.
                    await cur.execute('''
                        UPDATE research.analytical_jobs
                        SET status = 'RUNNING', started_at = NOW()
                        WHERE job_id = (
                            SELECT job_id 
                            FROM research.analytical_jobs 
                            WHERE status = 'PENDING' 
                            ORDER BY created_at ASC 
                            FOR UPDATE SKIP LOCKED 
                            LIMIT 1
                        )
                        RETURNING job_id, tenant_id, workload_type
                    ''')
                    job = await cur.fetchone()
                    await conn.commit()
                    
            if job:
                job_id, tenant_id, workload = job
                await process_job(job_id, tenant_id, workload)
            else:
                await asyncio.sleep(1.0)
                
        except Exception as e:
            print(f"[{WORKER_ID}] Scheduling Error: {str(e)}")
            await asyncio.sleep(2.0)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        print(f"[{WORKER_ID}] Terminating.")
