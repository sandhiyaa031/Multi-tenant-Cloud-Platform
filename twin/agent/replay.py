"""Replays captured production transactions against a clone and measures them.

Each transaction is started at the same offset from T0 at which it started in
production, as the same tenant role, with the same statements and parameters.
Like the workload driver, replay is open-loop: a transaction is launched on
schedule whether or not earlier ones have finished, and its latency is measured
from the scheduled time, so waiting for a free connection counts.

Besides the latency of each transaction, the time of each statement is kept per
query template (the query id production logged for it), so that a change which
moves one query and leaves the class percentile where it was can still be seen.
"""
import asyncio
import math
import os
import time
from collections import defaultdict
from datetime import datetime

import psycopg
from psycopg_pool import AsyncConnectionPool

from dbpilot_core.pglog import Transaction

POOL_SIZE = 16  # per tenant and class: the pooler's server-connection budget in production


def _percentile(sorted_values: list[float], p: float) -> float:
    return sorted_values[max(1, math.ceil(p / 100 * len(sorted_values))) - 1]


def summarise_templates(timings: dict[str, dict[str, list[float]]], texts: dict[str, str]) -> dict:
    """{"role/CLASS": {query id: {n, mean_ms, p50_ms, p95_ms, p99_ms, sql}}} from per-statement times."""
    out: dict[str, dict] = {}
    for key, by_template in timings.items():
        out[key] = {}
        for qid, values in by_template.items():
            values = sorted(values)
            out[key][qid] = {"n": len(values), "mean_ms": round(sum(values) / len(values), 4),
                             "p50_ms": round(_percentile(values, 50), 4), "p95_ms": round(_percentile(values, 95), 4),
                             "p99_ms": round(_percentile(values, 99), 4), "sql": texts.get(qid, "")}
    return out


class Replayer:
    """One clone's connection pools and what was measured on it. `run` may be called several
    times (once per block when two arms are interleaved); the results accumulate."""

    def __init__(self, port: int, conn_limits: dict[str, int]):
        self.port, self.conn_limits = port, conn_limits
        self.samples: dict[str, list] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self.examples: dict[str, str] = {}
        self.timings: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.texts: dict[str, str] = {}
        self._pools: dict[str, AsyncConnectionPool] = {}

    async def open(self, transactions: list[Transaction]) -> None:
        keys = sorted({(t.user, t.query_class) for t in transactions})
        per_role_classes: dict[str, int] = defaultdict(int)
        for role, _ in keys:
            per_role_classes[role] += 1
        for role, cls in keys:
            # A concurrency cap shrinks that tenant's pool, so its excess transactions queue
            # exactly as they would behind the production pooler.
            limit = self.conn_limits.get(role, -1)
            size = POOL_SIZE if limit < 0 else max(1, min(POOL_SIZE, limit // per_role_classes[role]))
            pool = AsyncConnectionPool(
                f"host=127.0.0.1 port={self.port} dbname=app user={role} application_name={cls.lower()}",
                min_size=min(2, size), max_size=size, open=False,
                kwargs={"autocommit": True, "prepare_threshold": None},
            )
            await pool.open(wait=True, timeout=60)
            self._pools[f"{role}/{cls}"] = pool

    async def _one(self, key: str, txn: Transaction) -> None:
        loop = asyncio.get_running_loop()
        async with self._pools[key].connection() as conn:
            async with conn.transaction():
                for statement in txn.statements:
                    began = loop.time()
                    await conn.execute(statement.literal_sql())
                    if statement.query_id is not None:
                        qid = str(statement.query_id)
                        self.timings[key][qid].append((loop.time() - began) * 1000)
                        if qid not in self.texts:
                            self.texts[qid] = statement.sql[:160]
                if txn.failed:
                    # It rolled back in production (for example the 1% of New-Orders that
                    # name a missing item); do the same work, then roll back here too.
                    raise psycopg.Rollback()

    async def run(self, transactions: list[Transaction], t0: datetime, origin: datetime | None = None) -> None:
        """Replays `transactions`, the first of them due `origin` (default `t0`) from now. Recorded
        offsets are always relative to `t0`, the start of the whole captured window."""
        origin = origin or t0
        loop = asyncio.get_running_loop()
        start = loop.time()
        tasks = []

        async def launch(txn: Transaction, due: float) -> None:
            key = f"{txn.user}/{txn.query_class}"
            try:
                await self._one(key, txn)
                offset = max(0.0, (txn.start - t0).total_seconds())
                self.samples[key].append((round(offset, 3), round((loop.time() - start - due) * 1000, 3)))
            except (psycopg.Error, asyncio.TimeoutError) as exc:
                self.errors[key] += 1
                self.examples.setdefault(key, f"{type(exc).__name__}: {str(exc)[:200]}")

        for txn in transactions:
            due = max(0.0, (txn.start - origin).total_seconds())
            delay = due - (loop.time() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(launch(txn, due)))
        if tasks:
            await asyncio.wait(tasks, timeout=300)

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()

    def result(self) -> dict:
        return {"samples": dict(self.samples), "errors": dict(self.errors), "error_examples": self.examples,
                "templates": summarise_templates(self.timings, self.texts)}


async def replay_full(transactions: list[Transaction], t0: datetime, port: int, conn_limits: dict[str, int]) -> dict:
    replayer = Replayer(port, conn_limits)
    try:
        await replayer.open(transactions)
        await replayer.run(transactions, t0)
    finally:
        await replayer.close()
    return replayer.result()


async def replay(transactions: list[Transaction], t0: datetime, port: int,
                 conn_limits: dict[str, int]) -> tuple[dict, dict, dict]:
    """Returns (samples, errors, error_examples) keyed by "role/CLASS"."""
    result = await replay_full(transactions, t0, port, conn_limits)
    return result["samples"], result["errors"], result["error_examples"]


def replay_in_process(transactions: list[Transaction], t0: datetime, port: int, conn_limits: dict[str, int],
                      start_at: float, cpu: int | None) -> dict:
    """Entry point of a replayer process, used when two arms are replayed at the same time: each
    arm gets its own process on its own core, and both start at the same wall-clock moment."""
    if cpu is not None:
        try:
            os.sched_setaffinity(0, {cpu})
        except (AttributeError, OSError):
            pass

    async def go() -> dict:
        replayer = Replayer(port, conn_limits)
        try:
            await replayer.open(transactions)
            await asyncio.sleep(max(0.0, start_at - time.time()))
            await replayer.run(transactions, t0)
        finally:
            await replayer.close()
        return replayer.result()

    return asyncio.run(go())
