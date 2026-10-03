"""Open-loop load generation and client-side measurement.

Open loop means arrivals follow a schedule that does not depend on how fast the
database answers, like real users. Latency is measured from the scheduled
arrival time, so time a request spends waiting for a free connection counts.
A closed-loop generator (send, wait, send) silently slows down when the
database is slow and under-reports exactly the latencies that matter; that
error is called coordinated omission.
"""
import asyncio
import json
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

import psycopg
from psycopg_pool import AsyncConnectionPool

from workload import olap, tpcc
from workload.tpcc import TenantCtx

MIXES = {"OLTP": tpcc.MIX, "OLAP": olap.MIX}


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    if not sorted_values:
        return float("nan")
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def burst_rate(base: float, burst: dict | None) -> Callable[[float], float]:
    """Arrival rate as a function of seconds since start; bursts recur periodically."""
    if not burst:
        return lambda elapsed: base

    def rate(elapsed: float) -> float:
        in_burst = (elapsed % burst["every_s"]) >= burst["every_s"] - burst["duration_s"]
        return base * burst["multiplier"] if in_burst else base

    return rate


@dataclass
class Recorder:
    """Collects latencies per (tenant, class, operation) and flushes them per interval."""

    out_path: str | None
    interval_s: float
    started: float = field(default_factory=time.time)
    _bucket: dict = field(default_factory=lambda: defaultdict(list))
    _errors: dict = field(default_factory=lambda: defaultdict(int))
    _dropped: dict = field(default_factory=lambda: defaultdict(int))
    totals: dict = field(default_factory=lambda: defaultdict(list))
    total_errors: dict = field(default_factory=lambda: defaultdict(int))
    total_dropped: dict = field(default_factory=lambda: defaultdict(int))

    def ok(self, key: tuple, latency_ms: float) -> None:
        self._bucket[key].append(latency_ms)
        self.totals[key[:2]].append(latency_ms)

    def error(self, key: tuple) -> None:
        self._errors[key] += 1
        self.total_errors[key[:2]] += 1

    def dropped(self, key: tuple) -> None:
        self._dropped[key] += 1
        self.total_dropped[key[:2]] += 1

    def flush(self) -> None:
        now = time.time()
        keys = set(self._bucket) | set(self._errors) | set(self._dropped)
        lines = []
        for tenant, cls, op in sorted(keys):
            values = sorted(self._bucket.get((tenant, cls, op), []))
            lines.append(
                json.dumps(
                    {
                        "t": round(now - self.started, 1),
                        "tenant": tenant,
                        "class": cls,
                        "op": op,
                        "count": len(values),
                        "errors": self._errors.get((tenant, cls, op), 0),
                        "dropped": self._dropped.get((tenant, cls, op), 0),
                        "p50_ms": round(percentile(values, 50), 3) if values else None,
                        "p95_ms": round(percentile(values, 95), 3) if values else None,
                        "p99_ms": round(percentile(values, 99), 3) if values else None,
                        "max_ms": round(values[-1], 3) if values else None,
                    }
                )
            )
        self._bucket.clear()
        self._errors.clear()
        self._dropped.clear()
        if self.out_path and lines:
            with open(self.out_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    def summary(self, duration_s: float) -> list[dict]:
        rows = []
        keys = set(self.totals) | set(self.total_errors) | set(self.total_dropped)
        for tenant, cls in sorted(keys):
            values = sorted(self.totals.get((tenant, cls), []))
            rows.append(
                {
                    "tenant": tenant,
                    "class": cls,
                    "completed": len(values),
                    "per_s": round(len(values) / duration_s, 2),
                    "errors": self.total_errors.get((tenant, cls), 0),
                    "dropped": self.total_dropped.get((tenant, cls), 0),
                    "p50_ms": round(percentile(values, 50), 2) if values else None,
                    "p95_ms": round(percentile(values, 95), 2) if values else None,
                    "p99_ms": round(percentile(values, 99), 2) if values else None,
                }
            )
        return rows


async def load_ctx(pool: AsyncConnectionPool, role: str) -> TenantCtx:
    """Discovers what the tenant can see, rather than trusting configuration."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT min(w_id), max(w_id) FROM ch.warehouse")
        w_lo, w_hi = await cur.fetchone()
        if w_lo is None:
            raise RuntimeError(f"tenant {role} sees no warehouses; is it provisioned and loaded?")
        cur = await conn.execute("SELECT max(c_id) FROM ch.customer WHERE c_w_id = %s AND c_d_id = 1", (w_lo,))
        customers = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT max(i_id) FROM ch.item")
        items = (await cur.fetchone())[0]
    return TenantCtx(role=role, w_lo=w_lo, w_hi=w_hi, customers_per_district=customers, items=items)


async def stream(
    ctx: TenantCtx,
    cls: str,
    rate: Callable[[float], float],
    pool: AsyncConnectionPool,
    recorder: Recorder,
    duration_s: float,
    max_inflight: int,
) -> None:
    """One Poisson arrival process for one tenant and class."""
    names, fns, weights = zip(*MIXES[cls])
    loop = asyncio.get_running_loop()
    start = loop.time()
    next_at = start
    inflight: set[asyncio.Task] = set()

    async def run_one(scheduled: float, name: str, fn) -> None:
        key = (ctx.role, cls, name)
        try:
            async with pool.connection() as conn:
                await fn(conn, ctx)
            recorder.ok(key, (loop.time() - scheduled) * 1000)
        except (psycopg.Error, asyncio.TimeoutError):
            recorder.error(key)

    while True:
        current = rate(next_at - start)
        next_at += random.expovariate(current) if current > 0 else 0.5
        if next_at - start >= duration_s:
            break
        delay = next_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if current <= 0:
            continue
        index = random.choices(range(len(names)), weights=weights)[0]
        if len(inflight) >= max_inflight:
            # The system is so far behind that queueing more would only exhaust memory. Count it.
            recorder.dropped((ctx.role, cls, names[index]))
            continue
        task = asyncio.create_task(run_one(next_at, names[index], fns[index]))
        inflight.add(task)
        task.add_done_callback(inflight.discard)

    if inflight:
        await asyncio.wait(inflight, timeout=60)


async def run(profile: dict, dsn_for: Callable[[str], str], duration_s: float, out_path: str | None,
              interval_s: float = 10.0, pool_size: int = 16, max_inflight: int = 200) -> list[dict]:
    recorder = Recorder(out_path=out_path, interval_s=interval_s)
    pools: dict[str, AsyncConnectionPool] = {}
    tasks = []
    try:
        for tenant in profile["tenants"]:
            role = tenant["role"]
            # prepare_threshold=None: no server-side prepared statements, which a
            # transaction-mode pooler cannot keep attached to one server connection.
            ctx = None
            for s in tenant["streams"]:
                # One pool per class, labelled with application_name the way a real
                # application tags its connections; DBPilot reads the label from the
                # statement log to separate OLTP from OLAP.
                pool = AsyncConnectionPool(
                    dsn_for(role) + f" application_name={s['class'].lower()}", min_size=2, max_size=pool_size,
                    open=False, kwargs={"prepare_threshold": None, "autocommit": True},
                )
                await pool.open(wait=True, timeout=30)
                pools[f"{role}/{s['class']}"] = pool
                ctx = ctx or await load_ctx(pool, role)
                tasks.append(
                    asyncio.create_task(
                        stream(ctx, s["class"], burst_rate(s["rate"], s.get("burst")), pool, recorder,
                               duration_s, max_inflight)
                    )
                )

        async def flusher() -> None:
            while True:
                await asyncio.sleep(interval_s)
                recorder.flush()

        flush_task = asyncio.create_task(flusher())
        await asyncio.gather(*tasks)
        flush_task.cancel()
        recorder.flush()
    finally:
        for pool in pools.values():
            await pool.close()
    return recorder.summary(duration_s)
