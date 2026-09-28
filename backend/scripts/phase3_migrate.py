"""
Phase 3: Creates research.routing_decisions telemetry table.
Run once before the Phase 3 experiment.
"""
import psycopg

conn = psycopg.connect("postgresql://postgres:100978@localhost:5432/postgres", autocommit=True)
cur = conn.cursor()

cur.execute("""
    CREATE TABLE IF NOT EXISTS research.routing_decisions (
        id          SERIAL PRIMARY KEY,
        job_id      UUID,
        timestamp   TIMESTAMP DEFAULT NOW(),
        target      TEXT,        -- 'replica' | 'primary'
        lag_bytes   BIGINT,
        lag_ms      REAL,
        reason      TEXT,
        exec_ms     REAL,
        success     BOOLEAN
    )
""")
print("research.routing_decisions table ensured.")
conn.close()
