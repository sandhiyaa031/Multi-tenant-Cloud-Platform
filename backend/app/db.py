import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import psycopg
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv

# Load .env file from the backend directory
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "postgres")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "postgres")
DB_ROLE = os.environ.get("DB_ROLE", "dbpilot_app")

CONN_INFO = f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} user={PG_USER} password={PG_PASSWORD}"

pool = AsyncConnectionPool(conninfo=CONN_INFO, open=False)

@asynccontextmanager
async def lifespan(app):
    """Manage the connection pool lifecycle tied to FastAPI."""
    await pool.open()
    yield
    await pool.close()

@asynccontextmanager
async def get_tenant_connection(tenant_id: str) -> AsyncGenerator[psycopg.AsyncConnection, None]:
    """
    Acquires a connection from the pool, sets the RLS transaction context,
    and yields it. The SET LOCAL app.tenant_id guarantees RLS isolation.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(f"SET ROLE {DB_ROLE};")
            await conn.execute("SELECT set_config('app.tenant_id', %s, true);", (tenant_id,))
            yield conn

async def get_raw_connection() -> AsyncGenerator[psycopg.AsyncConnection, None]:
    """
    Acquires a superuser-level connection (no RLS) for writing to the
    research schema tables from the middleware/controller.
    """
    async with pool.connection() as conn:
        yield conn
