import asyncio
import psycopg
import time
import sys
import json
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.db import pool, get_tenant_connection
from app.replica_db import routing_decision, get_replica_connection

# Phase 3: Enable replica routing by default. Set REPLICA_ROUTING=0 to disable.
USE_REPLICA_ROUTING = os.environ.get("REPLICA_ROUTING", "1") == "1"
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")

async def _log_routing(job_id, target, lag_bytes, lag_ms, reason, exec_ms, success):
    """Log routing decision to research.routing_decisions (superuser, orchestration only)."""
    try:
        async with pool.connection() as conn:
            await conn.execute("""
                INSERT INTO research.routing_decisions
                    (job_id, target, lag_bytes, lag_ms, reason, exec_ms, success)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (job_id, target, lag_bytes, lag_ms, reason, exec_ms, success))
    except Exception as e:
        print(f"[{WORKER_ID}] routing log error (non-fatal): {e}")


async def process_job(job_id: str, tenant_id: str, workload: str):
    print(f"[{WORKER_ID}] Claimed Job {job_id} for Tenant {tenant_id}")

    # Phase 3: Determine execution target BEFORE acquiring workload connection.
    # routing_decision() uses a superuser monitoring connection to pg_stat_replication.
    # It NEVER executes analytical queries — only reads lag metadata.
    if USE_REPLICA_ROUTING:
        target, lag_bytes, lag_ms, reason = await routing_decision()
    else:
        target, lag_bytes, lag_ms, reason = "primary", -1, -1.0, "routing_disabled"

    print(f"[{WORKER_ID}] Routing target={target} lag_ms={lag_ms:.2f} reason={reason}")

    # Select the connection context based on routing decision.
    # get_replica_connection() enforces SET LOCAL ROLE dbpilot_app + SET LOCAL app.tenant_id
    # on the replica — same RLS model as get_tenant_connection() on the primary.
    conn_ctx = get_replica_connection(tenant_id) if target == "replica" else get_tenant_connection(tenant_id)

    try:
        async with conn_ctx as conn:
            async with conn.cursor() as cur:
                print(f"[{WORKER_ID}] Executing analytical workload on {target}...")
                start_t = time.time()

                # Read-only aggregation query — confirmed no writes.
                await cur.execute('''
                    SELECT source, count(*) as flow_count, sum(orig_bytes) as total_bytes
                    FROM app.security_events
                    GROUP BY source
                    ORDER BY flow_count DESC
                ''')
                rows = await cur.fetchall()
                elapsed = time.time() - start_t

                payload = {
                    "worker_id": WORKER_ID,
                    "results_count": len(rows),
                    "execution_s": round(elapsed, 4),
                    "target": target,
                    "lag_bytes": lag_bytes,
                    "lag_ms": lag_ms,
                    "routing_reason": reason,
                }

        # Job status update goes to PRIMARY via superuser pool — replica is read-only.
        async with pool.connection() as p_conn:
            await p_conn.execute('''
                UPDATE research.analytical_jobs
                SET status = 'COMPLETED',
                    completed_at = NOW(),
                    result_metadata = %s
                WHERE job_id = %s
            ''', (json.dumps(payload), job_id))
            await p_conn.commit()

        await _log_routing(job_id, target, lag_bytes, lag_ms, reason, elapsed * 1000, True)
        print(f"[{WORKER_ID}] Job {job_id} COMPLETED on {target} in {elapsed:.2f}s.")
                
    except Exception as e:
        err_msg = str(e)
        print(f"[{WORKER_ID}] Job {job_id} FAILED: {err_msg}")
        
        try:
            # Re-acquire tenant connection strictly for the failure update
            async with get_tenant_connection(tenant_id) as conn:
                async with conn.cursor() as cur:
                    await cur.execute('''
                        UPDATE research.analytical_jobs 
                        SET status = 'FAILED', 
                            completed_at = NOW(),
                            error_info = %s
                        WHERE job_id = %s
                    ''', (err_msg, job_id))
                    # Auto-committed by transaction context
        except Exception as fallback_err:
            print(f"[{WORKER_ID}] FATAL: Unable to mark job as FAILED. {fallback_err}")


async def worker_loop():
    print(f"[{WORKER_ID}] Opening async DB pool...")
    await pool.open()
    
    print(f"[{WORKER_ID}] Elastic Analytical Compute Worker Booted.")
    print(f"[{WORKER_ID}] Polling for PENDING jobs...")
    
    try:
        while True:
            try:
                # Privileged Polling: The worker acts as the global queue scheduler.
                # It uses the system connection pool default (superuser) ONLY to fetch 
                # effectively locking a job to prevent duplicate execution.
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
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
                traceback.print_exc()
                await asyncio.sleep(2.0)
    finally:
        await pool.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        print(f"[{WORKER_ID}] Terminating.")
