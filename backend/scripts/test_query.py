import asyncio
import psycopg
import os
from dotenv import load_dotenv

load_dotenv('backend/.env')

async def run():
    conn_str = f"postgresql://{os.getenv('PG_USER')}:{os.getenv('PG_PASSWORD')}@{os.getenv('PG_HOST')}:{os.getenv('PG_PORT')}/{os.getenv('PG_DB')}"
    conn = await psycopg.AsyncConnection.connect(conn_str)
    cur = conn.cursor()
    
    print("--- 1. DATABASE VALIDATION ---")
    await cur.execute("SELECT COUNT(*) FROM research.ctu_conn_log")
    print('Rows Loaded:', (await cur.fetchone())[0])
    
    await cur.execute("SELECT COUNT(DISTINCT id_orig_h), COUNT(DISTINCT id_resp_h) FROM research.ctu_conn_log")
    r = await cur.fetchone()
    print(f'Distinct Source IPs: {r[0]} | Distinct Dest IPs: {r[1]}')
    
    await cur.execute("SELECT proto, COUNT(*) FROM research.ctu_conn_log GROUP BY proto")
    print('Protocols:', await cur.fetchall())
    
    await cur.execute("SELECT COUNT(*) FROM research.ctu_conn_log WHERE duration IS NULL")
    print('Null Durations:', (await cur.fetchone())[0])
    
    await cur.execute("SELECT min(ts), max(ts) FROM research.ctu_conn_log")
    r = await cur.fetchone()
    print(f'Time Range: {r[0]} to {r[1]}')
    
    await cur.execute("SELECT MIN(duration), MAX(duration), AVG(duration) FROM research.ctu_conn_log WHERE duration IS NOT NULL")
    r = await cur.fetchone()
    print(f"Duration Stats (seconds): Min {r[0]:.4f}, Max {r[1]:.4f}, Avg {r[2]:.4f}")

    print("\\n--- 2. DATABASE SIZE ---")
    await cur.execute("SELECT pg_size_pretty(pg_relation_size('research.ctu_conn_log')) AS table_size, pg_size_pretty(pg_indexes_size('research.ctu_conn_log')) AS index_size, pg_size_pretty(pg_total_relation_size('research.ctu_conn_log')) AS total_size;")
    size = await cur.fetchone()
    print(f"Table: {size[0]} | Index: {size[1]} | Total: {size[2]}")

    print("\\n--- 3. QUERY VALIDATION (EXPLAIN ANALYZE) ---")
    
    # INTERACTIVE QUERY
    await cur.execute("EXPLAIN ANALYZE SELECT uid, conn_state FROM research.ctu_conn_log WHERE id_orig_h = '118.123.0.0' AND ts > '2024-05-15' AND ts < '2024-05-16'")
    print("--- Interactive Query Plan ---")
    for row in await cur.fetchall():
        print(row[0])
        
    # ANALYTICAL QUERY
    await cur.execute("EXPLAIN ANALYZE SELECT id_resp_p, sum(orig_ip_bytes), count(*) FROM research.ctu_conn_log GROUP BY id_resp_p ORDER BY count DESC LIMIT 10")
    print("\\n--- Analytical Query Plan ---")
    for row in await cur.fetchall():
        print(row[0])


    await conn.close()

import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(run())
