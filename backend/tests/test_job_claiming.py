import asyncio
import psycopg
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import pool, CONN_INFO

async def claim_job(worker_name, claimed_set):
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
                RETURNING job_id
            ''')
            job = await cur.fetchone()
            await conn.commit()
            if job:
                claimed_set.add((worker_name, job[0]))
                return True
            return False

async def worker_sim(worker_name, claimed_set):
    while True:
        job_found = await claim_job(worker_name, claimed_set)
        if not job_found:
            break

async def main():
    await pool.open()
    try:
        # Clean jobs
        async with pool.connection() as conn:
            await conn.execute("TRUNCATE table research.analytical_jobs;")
            await conn.commit()
            
            # Insert 5 jobs
            tenant_id = "00000000-0000-0000-0000-000000000000"
            for _ in range(5):
                await conn.execute("INSERT INTO research.analytical_jobs (tenant_id, workload_type, routing_decision, status) VALUES (%s, %s, %s, 'PENDING')", (tenant_id, "analytical", "OFFLOADED"))
            await conn.commit()
            
        claimed_jobs = set()
        
        # Run two workers concurrently
        await asyncio.gather(
            worker_sim("A", claimed_jobs),
            worker_sim("B", claimed_jobs)
        )
        
        # Verify
        ids = [j[1] for j in claimed_jobs]
        assert len(ids) == 5, f"Expected 5 claimed jobs, got {len(ids)}"
        assert len(set(ids)) == 5, "Duplicate job ID claimed!"
        
        print("Concurrency test passed! 5 jobs claimed safely by multiple concurrent workers.")
        
    finally:
        await pool.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
