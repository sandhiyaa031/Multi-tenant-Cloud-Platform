"""Action-sensitivity screen: can an action move tenant latency at all, on this stack?

For each condition the action is really applied and reverted around alternating blocks (counterbalanced AB/BA)
while one tenant runs the real transaction or query code through the pooler. It measures the effect of a single
action on a single class with none of the noise of a loaded production run. It does not replace a production
trial; it says whether a production trial could possibly see an effect.

    python -m evaluation.screen --out /results/p01b_screen.jsonl          # needs DP_OWNER_URL
"""
import argparse
import asyncio
import json
import os
import random
import statistics
import time
from collections import defaultdict

import psycopg
from psycopg_pool import AsyncConnectionPool

from workload import olap, tpcc
from workload.driver import load_ctx, percentile

ROUNDS, OLTP_N, OLAP_REPS, WARM_OLTP = 6, 150, 6, 15
INDEX_COLS = {"order_line": "ol_i_id, ol_amount, ol_quantity, ol_delivery_d", "stock": "s_quantity"}


def setting(name: str, value: str) -> dict:
    return {"apply": [f"ALTER SYSTEM SET {name} = '{value}'", "SELECT pg_reload_conf()"],
            "revert": [f"ALTER SYSTEM RESET {name}", "SELECT pg_reload_conf()"]}


def index(table: str, roles: list[str]) -> dict:
    names = [(f"screen_{table}_{r}", f"{table}_{r}") for r in roles]
    return {"apply": [f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {n} ON ch.{rel} ({INDEX_COLS[table]})" for n, rel in names],
            "revert": [f"DROP INDEX CONCURRENTLY IF EXISTS ch.{n}" for n, _ in names]}


def conditions(roles: list[str]) -> dict:
    safe = ["ALTER FUNCTION ch.my_w_lo() PARALLEL SAFE", "ALTER FUNCTION ch.my_w_hi() PARALLEL SAFE"]
    unsafe = ["ALTER FUNCTION ch.my_w_lo() PARALLEL UNSAFE", "ALTER FUNCTION ch.my_w_hi() PARALLEL UNSAFE"]
    return {
        "AA": {"apply": [], "revert": []},
        "S6_index_order_line_all": index("order_line", roles),
        "S10_parallel_off": setting("max_parallel_workers_per_gather", "0"),
        "S10p_parallel_off_rls_safe": {**setting("max_parallel_workers_per_gather", "0"), "setup": safe, "teardown": unsafe},
        "work_mem_1MB": setting("work_mem", "1MB"),
        "random_page_cost_1": setting("random_page_cost", "1.0"),
        "index_stock_s_quantity_all": index("stock", roles),
    }


async def oltp_block(pool, ctx, n: int) -> tuple[list[float], dict, int]:
    names, fns, weights = zip(*tpcc.MIX)
    lat, per, errors = [], defaultdict(list), 0
    for _ in range(n):
        i = random.choices(range(len(names)), weights=weights)[0]
        start = time.perf_counter()
        try:
            async with pool.connection() as conn:
                await fns[i](conn, ctx)
        except psycopg.Error:
            errors += 1
            continue
        ms = (time.perf_counter() - start) * 1000
        lat.append(ms)
        per[names[i]].append(ms)
    return lat, per, errors


async def olap_block(pool, ctx, reps: int) -> tuple[list[float], dict, int]:
    lat, per, errors = [], defaultdict(list), 0
    order = [name for name in olap.QUERIES for _ in range(reps)]
    random.shuffle(order)
    for name in order:
        start = time.perf_counter()
        try:
            async with pool.connection() as conn:
                await olap.make(name)(conn, ctx)
        except psycopg.Error:
            errors += 1
            continue
        ms = (time.perf_counter() - start) * 1000
        lat.append(ms)
        per[name].append(ms)
    return lat, per, errors


async def plan_has_gather(pool) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("EXPLAIN (COSTS OFF) SELECT sum(ol_amount) FROM ch.order_line"
                                 " WHERE ol_delivery_d >= '2007-01-02' AND ol_quantity BETWEEN 1 AND 100000")
        return any("Gather" in r[0] for r in await cur.fetchall())


def p95(values: list[float]) -> float:
    return percentile(sorted(values), 95)


async def main_async(args) -> None:
    owner = await psycopg.AsyncConnection.connect(os.environ["DP_OWNER_URL"], autocommit=True)
    cur = await owner.execute("SELECT db_role FROM ch.tenant_map ORDER BY w_lo")
    roles = [r[0] for r in await cur.fetchall()]
    host, password = os.environ.get("DP_POOLER_HOST", "pgbouncer"), os.environ["DP_TENANT_PASSWORD"]

    pools = {}
    for role in ("t_steady", "t_analytic"):
        dsn = f"host={host} port=6432 dbname=app user={role} password={password}"
        pools[role] = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False,
                                          kwargs={"prepare_threshold": None, "autocommit": True})
        await pools[role].open(wait=True, timeout=30)
    ctx = {role: await load_ctx(pools[role], role) for role in pools}
    cur = await owner.execute("SELECT proparallel FROM pg_proc WHERE proname = 'my_w_lo'")
    original_parallel = (await cur.fetchone())[0]

    async def run_sql(statements: list[str]) -> None:
        for statement in statements:
            await owner.execute(statement)
        await asyncio.sleep(1.0)

    try:
        for name, cond in conditions(roles).items():
            if args.only and name not in args.only.split(","):
                continue
            await run_sql(cond.get("setup", []))
            state, gather = "A", {}
            try:
                for rnd in range(ROUNDS):
                    for arm in (["A", "B"] if rnd % 2 == 0 else ["B", "A"]):
                        if arm != state:
                            await run_sql(cond["apply"] if arm == "B" else cond["revert"])
                            state = arm
                        await oltp_block(pools["t_steady"], ctx["t_steady"], WARM_OLTP)
                        await olap_block(pools["t_analytic"], ctx["t_analytic"], 1)
                        if arm not in gather:
                            gather[arm] = await plan_has_gather(pools["t_analytic"])
                        o_lat, o_per, o_err = await oltp_block(pools["t_steady"], ctx["t_steady"], OLTP_N)
                        a_lat, a_per, a_err = await olap_block(pools["t_analytic"], ctx["t_analytic"], OLAP_REPS)
                        row = {"condition": name, "round": rnd, "arm": arm, "ts": time.time(),
                               "t_steady/OLTP": {"p95": p95(o_lat), "n": len(o_lat), "errors": o_err,
                                                 "p50_by_op": {k: statistics.median(v) for k, v in o_per.items()}},
                               "t_analytic/OLAP": {"p95": p95(a_lat), "n": len(a_lat), "errors": a_err,
                                                   "p50_by_op": {k: statistics.median(v) for k, v in a_per.items()}},
                               "gather_in_olap_plan": gather[arm]}
                        with open(args.out, "a", encoding="utf-8") as f:
                            f.write(json.dumps(row) + "\n")
                        print(f"{name:28} r{rnd} {arm} OLTP p95 {row['t_steady/OLTP']['p95']:6.1f}"
                              f"  OLAP p95 {row['t_analytic/OLAP']['p95']:6.1f}  gather={gather[arm]}", flush=True)
            finally:
                if state == "B":
                    await run_sql(cond["revert"])
                await run_sql(cond.get("teardown", []))
    finally:
        flag = "SAFE" if original_parallel == "s" else "UNSAFE"
        await owner.execute(f"ALTER FUNCTION ch.my_w_lo() PARALLEL {flag}")
        await owner.execute(f"ALTER FUNCTION ch.my_w_hi() PARALLEL {flag}")
        for pool in pools.values():
            await pool.close()
        await owner.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/results/p01b_screen.jsonl")
    parser.add_argument("--only", help="comma-separated condition names")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
