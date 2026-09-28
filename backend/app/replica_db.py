"""
backend/app/replica_db.py

Phase 3: Replica-Aware Routing Module

Provides:
  - get_replica_connection(tenant_id): RLS-aware async context manager targeting port 5433
  - measure_replica_lag(): returns (lag_bytes, lag_ms, state) from pg_stat_replication
  - routing_decision(): returns (target, lag_bytes, lag_ms, reason)

Lag definition:
  lag_ms  = EXTRACT(EPOCH FROM replay_lag) * 1000
             where replay_lag is PostgreSQL's time between WAL generation on
             the primary and its application on the standby (from pg_stat_replication).
  lag_bytes = sent_lsn - replay_lsn (bytes of WAL not yet applied on replica).

Staleness budget:
  Routing to replica is allowed only when lag_ms < STALENESS_BUDGET_MS.
  If pg_stat_replication returns no rows (replica disconnected), falls back to primary.

Security:
  Analytical workload queries use get_replica_connection() which enforces:
    SET LOCAL ROLE dbpilot_app
    SET LOCAL app.tenant_id = <tenant_id>
  Queue orchestration and lag monitoring use superuser connections, scoped
  to those operations only — they never execute analytical queries.
"""

import asyncio
import os
from contextlib import asynccontextmanager
import psycopg

# ── Configuration ────────────────────────────────────────────────────────────
ENABLED = True           # Set False to force all traffic to primary
STALENESS_BUDGET_MS = 2000

# Replica DSN — port 5433, same host/credentials as primary
_PG_HOST = os.environ.get("PG_HOST", "127.0.0.1")
_PG_USER = os.environ.get("PG_USER", "postgres")
_PG_PASSWORD = os.environ.get("PG_PASSWORD", "100978")
_PG_DB = os.environ.get("PG_DB", "postgres")

REPLICA_CONNINFO = (
    f"host={_PG_HOST} port=5433 user={_PG_USER} "
    f"password={_PG_PASSWORD} dbname={_PG_DB}"
)
PRIMARY_MONITOR_CONNINFO = (
    f"host={_PG_HOST} port=5432 user={_PG_USER} "
    f"password={_PG_PASSWORD} dbname={_PG_DB}"
)


# ── Lag measurement ──────────────────────────────────────────────────────────
async def measure_replica_lag() -> tuple[int, float, str]:
    """
    Queries pg_stat_replication on the PRIMARY as superuser (monitoring only).
    Returns (lag_bytes, lag_ms, state):
      lag_bytes: bytes of WAL sent but not yet replayed on replica
      lag_ms:    EXTRACT(EPOCH FROM replay_lag) * 1000 — PG-native time-based lag
      state:     replica streaming state string ('streaming', 'catchup', etc.)
    Returns (-1, -1.0, 'no_row') if no active replication connection is found.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(PRIMARY_MONITOR_CONNINFO)
        async with conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT
                        state,
                        CASE
                            WHEN replay_lag IS NOT NULL THEN (EXTRACT(EPOCH FROM replay_lag) * 1000)
                            WHEN sent_lsn IS NOT NULL AND sent_lsn = replay_lsn THEN 0.0
                            ELSE -1.0
                        END AS lag_ms,
                        CASE
                            WHEN sent_lsn IS NOT NULL AND replay_lsn IS NOT NULL
                            THEN (sent_lsn - replay_lsn)
                            ELSE -1
                        END AS lag_bytes
                    FROM pg_stat_replication
                    ORDER BY lag_ms DESC NULLS LAST
                    LIMIT 1
                """)
                row = await cur.fetchone()
                if row is None:
                    return (-1, -1.0, "no_row")
                state, lag_ms, lag_bytes = row
                lag_ms = float(lag_ms) if lag_ms is not None else -1.0
                lag_bytes = int(lag_bytes) if lag_bytes is not None else -1
                return (lag_bytes, lag_ms, str(state))
    except Exception as e:
        return (-1, -1.0, f"error:{e}")


# ── Routing decision ─────────────────────────────────────────────────────────
async def routing_decision() -> tuple[str, int, float, str]:
    """
    Returns (target, lag_bytes, lag_ms, reason):
      target: 'replica' | 'primary'
    """
    if not ENABLED:
        return ("primary", -1, -1.0, "replica_disabled")

    lag_bytes, lag_ms, state = await measure_replica_lag()

    if state == "no_row":
        return ("primary", -1, -1.0, "no_active_replication")
    if state.startswith("error:"):
        return ("primary", -1, -1.0, state)
    if lag_ms < 0:
        return ("primary", lag_bytes, lag_ms, "lag_unavailable")
    if lag_ms >= STALENESS_BUDGET_MS:
        return ("primary", lag_bytes, lag_ms, f"lag_{lag_ms:.1f}ms_exceeds_budget_{STALENESS_BUDGET_MS}ms")

    return ("replica", lag_bytes, lag_ms, f"lag_{lag_ms:.1f}ms_within_budget")


# ── Tenant-aware replica connection (RLS-enforced) ───────────────────────────
@asynccontextmanager
async def get_replica_connection(tenant_id: str):
    """
    Async context manager providing an RLS-enforced connection to the read replica.
    Mirrors get_tenant_connection() from db.py but targets port 5433.

    Security:
      Uses SET LOCAL ROLE dbpilot_app and SET LOCAL app.tenant_id within a
      transaction, so every query is tenant-scoped and cannot access other tenants.
      Replica is read-only at the PostgreSQL instance level; any write attempt
      will raise ReadOnlySQLTransaction.
    """
    conn = await psycopg.AsyncConnection.connect(REPLICA_CONNINFO)
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SET LOCAL ROLE dbpilot_app")
                await cur.execute("SET LOCAL app.tenant_id = %s", (str(tenant_id),))
            yield conn
    finally:
        await conn.close()
