from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings

pool = AsyncConnectionPool(
    conninfo=get_settings().database_url,
    open=False,
    min_size=2,
    max_size=10,
    kwargs={"row_factory": dict_row},
)


@asynccontextmanager
async def system_tx() -> AsyncIterator[AsyncConnection]:
    """A transaction with no organization context.

    Row-level security hides every org-owned row here, so this is only useful
    for the SECURITY DEFINER auth functions (signup, login lookup, reset).
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            yield conn


@asynccontextmanager
async def org_tx(user_id: UUID, org_id: UUID) -> AsyncIterator[AsyncConnection]:
    """A transaction bound to one user acting in one organization.

    set_config(..., true) is transaction-local: the identity disappears at
    COMMIT/ROLLBACK, so the next request that borrows this pooled connection
    starts with no identity at all.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', %s, true), set_config('app.org_id', %s, true)",
                (str(user_id), str(org_id)),
            )
            yield conn
