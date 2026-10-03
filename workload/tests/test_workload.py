import asyncio
import os

import psycopg
import pytest

from workload import olap, tpcc
from workload.driver import Recorder, burst_rate, load_ctx, percentile, run

HOST = os.environ.get("DP_POOLER_HOST", "pgbouncer")
PASSWORD = os.environ["DP_TENANT_PASSWORD"]


def dsn(role: str) -> str:
    return f"host={HOST} port=6432 dbname=app user={role} password={PASSWORD}"


def test_percentile_is_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile([7.0], 99) == 7.0


def test_burst_rate_schedule():
    rate = burst_rate(10, {"every_s": 60, "duration_s": 15, "multiplier": 5})
    assert rate(0) == 10 and rate(44.9) == 10
    assert rate(45) == 50 and rate(59.9) == 50
    assert rate(60) == 10 and rate(105) == 50
    assert burst_rate(3, None)(1234) == 3


def test_nurand_stays_in_range_and_last_name_matches_loader():
    assert all(1 <= tpcc.nurand(1023, 1, 3000) <= 3000 for _ in range(5000))
    assert tpcc.last_name(0) == "BARBARBAR"
    assert tpcc.last_name(371) == "PRICALLYOUGHT"


def test_recorder_measures_per_tenant_and_class():
    rec = Recorder(out_path=None, interval_s=10)
    for ms in (1.0, 2.0, 3.0):
        rec.ok(("a", "OLTP", "payment"), ms)
    rec.error(("a", "OLTP", "payment"))
    rec.ok(("b", "OLAP", "q1"), 100.0)
    rows = {(r["tenant"], r["class"]): r for r in rec.summary(duration_s=1)}
    assert rows[("a", "OLTP")]["completed"] == 3 and rows[("a", "OLTP")]["errors"] == 1
    assert rows[("b", "OLAP")]["p99_ms"] == 100.0


async def _ctx(role: str) -> tuple[psycopg.AsyncConnection, tpcc.TenantCtx]:
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(dsn(role), min_size=1, max_size=1, open=False, kwargs={"autocommit": True})
    await pool.open(wait=True)
    try:
        ctx = await load_ctx(pool, role)
    finally:
        await pool.close()
    conn = await psycopg.AsyncConnection.connect(dsn(role), autocommit=True, prepare_threshold=None)
    return conn, ctx


def test_context_is_discovered_from_what_the_tenant_can_see():
    async def go():
        conn, ctx = await _ctx("t_analytic")
        await conn.close()
        return ctx

    ctx = asyncio.run(go())
    assert (ctx.w_lo, ctx.w_hi) == (5, 8)
    assert ctx.customers_per_district >= 30 and ctx.items >= 1000


@pytest.mark.parametrize("name,fn,_weight", tpcc.MIX, ids=[m[0] for m in tpcc.MIX])
def test_each_tpcc_transaction_runs(name, fn, _weight):
    async def go():
        conn, ctx = await _ctx("t_steady")
        try:
            for _ in range(5):
                await fn(conn, ctx)
        finally:
            await conn.close()

    asyncio.run(go())


@pytest.mark.parametrize("name", list(olap.QUERIES))
def test_each_analytical_query_runs(name):
    async def go():
        conn, ctx = await _ctx("t_steady")
        try:
            await olap.make(name)(conn, ctx)
        finally:
            await conn.close()

    asyncio.run(go())


def test_new_order_advances_the_district_counter_and_writes_consistent_rows():
    async def go():
        conn, ctx = await _ctx("t_steady")
        try:
            before = (await (await conn.execute("SELECT sum(d_next_o_id) FROM ch.district")).fetchone())[0]
            orders = (await (await conn.execute("SELECT count(*) FROM ch.orders")).fetchone())[0]
            for _ in range(20):
                await tpcc.new_order(conn, ctx)
            after = (await (await conn.execute("SELECT sum(d_next_o_id) FROM ch.district")).fetchone())[0]
            orders_after = (await (await conn.execute("SELECT count(*) FROM ch.orders")).fetchone())[0]
            mismatched = (await (await conn.execute(
                "SELECT count(*) FROM ch.orders o WHERE o_ol_cnt <> (SELECT count(*) FROM ch.order_line"
                " WHERE ol_w_id = o_w_id AND ol_d_id = o_d_id AND ol_o_id = o_id)")).fetchone())[0]
        finally:
            await conn.close()
        return before, after, orders, orders_after, mismatched

    before, after, orders, orders_after, mismatched = asyncio.run(go())
    # Rolled-back orders (1%) leave no trace, so the counter and the order count move together.
    assert after - before == orders_after - orders
    assert 18 <= orders_after - orders <= 20
    assert mismatched == 0


def test_open_loop_run_hits_its_target_rate():
    profile = {"tenants": [{"role": "t_steady", "streams": [{"class": "OLTP", "rate": 30}]}]}
    summary = asyncio.run(run(profile, dsn, duration_s=10, out_path=None, interval_s=5))
    row = summary[0]
    assert row["errors"] == 0 and row["dropped"] == 0
    # Poisson arrivals at 30/s for 10 s: mean 300, standard deviation ~17.
    assert 230 <= row["completed"] <= 370
