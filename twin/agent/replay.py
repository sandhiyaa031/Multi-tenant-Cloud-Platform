"""Replays captured production transactions against a clone and measures them.

Each transaction is started at the same offset from T0 at which it started in
production, as the same tenant role, with the same statements and parameters.
Like the workload driver, replay is open-loop: a transaction is launched on
schedule whether or not earlier ones have finished, and its latency is measured
from the scheduled time, so waiting for a free connection counts.
"""
import asyncio
from collections import defaultdict
from datetime import datetime

import psycopg
from psycopg_pool import AsyncConnectionPool

from dbpilot_core.pglog import Transaction

POOL_SIZE = 16  # per tenant and class: the pooler's server-connection budget in production


async def _one(pool: AsyncConnectionPool, txn: Transaction) -> None:
    async with pool.connection() as conn:
        async with conn.transaction():
            for statement in txn.statements:
                await conn.execute(statement.literal_sql())
            if txn.failed:
                # It rolled back in production (for example the 1% of New-Orders that
                # name a missing item); do the same work, then roll back here too.
                raise psycopg.Rollback()


async def replay(transactions: list[Transaction], t0: datetime, port: int,
                 conn_limits: dict[str, int]) -> tuple[dict, dict, dict]:
    """Returns (samples, errors, error_examples) keyed by "role/CLASS".

    `conn_limits` is each role's connection limit on the clone (-1 for none): a
    concurrency cap shrinks that tenant's pool, so its excess transactions queue
    exactly as they would behind the production pooler.
    """
    samples: dict[str, list] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    examples: dict[str, str] = {}
    pools: dict[str, AsyncConnectionPool] = {}
    loop = asyncio.get_running_loop()

    keys = sorted({(t.user, t.query_class) for t in transactions})
    per_role_classes = defaultdict(int)
    for role, _ in keys:
        per_role_classes[role] += 1
    for role, cls in keys:
        limit = conn_limits.get(role, -1)
        size = POOL_SIZE if limit < 0 else max(1, min(POOL_SIZE, limit // per_role_classes[role]))
        pool = AsyncConnectionPool(
            f"host=127.0.0.1 port={port} dbname=app user={role} application_name={cls.lower()}",
            min_size=min(2, size), max_size=size, open=False,
            kwargs={"autocommit": True, "prepare_threshold": None},
        )
        await pool.open(wait=True, timeout=60)
        pools[f"{role}/{cls}"] = pool

    start = loop.time()
    tasks = []

    async def launch(txn: Transaction, offset: float) -> None:
        key = f"{txn.user}/{txn.query_class}"
        try:
            await _one(pools[key], txn)
            samples[key].append((round(offset, 3), round((loop.time() - start - offset) * 1000, 3)))
        except (psycopg.Error, asyncio.TimeoutError) as exc:
            errors[key] += 1
            examples.setdefault(key, f"{type(exc).__name__}: {str(exc)[:200]}")

    try:
        for txn in transactions:
            offset = max(0.0, (txn.start - t0).total_seconds())
            delay = offset - (loop.time() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(launch(txn, offset)))
        if tasks:
            await asyncio.wait(tasks, timeout=300)
    finally:
        for pool in pools.values():
            await pool.close()
    return dict(samples), dict(errors), examples
