"""Analytical queries over the TPC-C tables.

Q1, Q3, Q4, Q6, Q12 and Q14 are adapted from the CH-benCHmark query set (which
restates TPC-H queries against the TPC-C schema). `item_sales` is not part of
CH: it is an ad hoc lookup by item, included because order_line has no index on
ol_i_id, which makes it the natural subject of index-related scenarios.

Row-level security adds the tenant's warehouse range to every query, so the
same text scans only the calling tenant's partitions.
"""
import random
from datetime import datetime, timezone

import psycopg

from workload.tpcc import TenantCtx

# CH uses a fixed early date as the lower bound, so the filters select nearly all rows.
EPOCH = datetime(2007, 1, 2, tzinfo=timezone.utc)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=timezone.utc)

QUERIES = {
    "q1_pricing_summary": (
        "SELECT ol_number, sum(ol_quantity) AS sum_qty, sum(ol_amount) AS sum_amount,"
        " avg(ol_quantity) AS avg_qty, avg(ol_amount) AS avg_amount, count(*) AS count_order"
        " FROM ch.order_line WHERE ol_delivery_d > %s GROUP BY ol_number ORDER BY ol_number",
        lambda ctx: (EPOCH,),
    ),
    "q3_unshipped_revenue": (
        "SELECT ol_o_id, ol_w_id, ol_d_id, sum(ol_amount) AS revenue, o_entry_d"
        " FROM ch.customer, ch.new_order, ch.orders, ch.order_line"
        " WHERE c_state LIKE %s AND c_id = o_c_id AND c_w_id = o_w_id AND c_d_id = o_d_id"
        " AND no_w_id = o_w_id AND no_d_id = o_d_id AND no_o_id = o_id"
        " AND ol_w_id = o_w_id AND ol_d_id = o_d_id AND ol_o_id = o_id AND o_entry_d > %s"
        " GROUP BY ol_o_id, ol_w_id, ol_d_id, o_entry_d ORDER BY revenue DESC, o_entry_d LIMIT 100",
        lambda ctx: (random.choice("0123456789ABCDEF") + "%", EPOCH),
    ),
    "q4_order_priority": (
        "SELECT o_ol_cnt, count(*) AS order_count FROM ch.orders"
        " WHERE o_entry_d >= %s AND o_entry_d < %s AND EXISTS ("
        "   SELECT 1 FROM ch.order_line WHERE o_id = ol_o_id AND o_w_id = ol_w_id AND o_d_id = ol_d_id"
        "   AND ol_delivery_d >= o_entry_d)"
        " GROUP BY o_ol_cnt ORDER BY o_ol_cnt",
        lambda ctx: (EPOCH, FAR_FUTURE),
    ),
    "q6_revenue_forecast": (
        "SELECT sum(ol_amount) AS revenue FROM ch.order_line"
        " WHERE ol_delivery_d >= %s AND ol_delivery_d < %s AND ol_quantity BETWEEN %s AND %s",
        lambda ctx: (EPOCH, FAR_FUTURE, 1, 100000),
    ),
    "q12_shipping_modes": (
        "SELECT o_ol_cnt,"
        " sum(CASE WHEN o_carrier_id = 1 OR o_carrier_id = 2 THEN 1 ELSE 0 END) AS high_line_count,"
        " sum(CASE WHEN o_carrier_id <> 1 AND o_carrier_id <> 2 THEN 1 ELSE 0 END) AS low_line_count"
        " FROM ch.orders, ch.order_line"
        " WHERE ol_w_id = o_w_id AND ol_d_id = o_d_id AND ol_o_id = o_id"
        " AND o_entry_d <= ol_delivery_d AND ol_delivery_d < %s"
        " GROUP BY o_ol_cnt ORDER BY o_ol_cnt",
        lambda ctx: (FAR_FUTURE,),
    ),
    "q14_promotion_effect": (
        "SELECT 100.00 * sum(CASE WHEN i_data LIKE 'PR%%' THEN ol_amount ELSE 0 END) / (1 + sum(ol_amount))"
        " AS promo_revenue FROM ch.order_line, ch.item"
        " WHERE ol_i_id = i_id AND ol_delivery_d >= %s AND ol_delivery_d < %s",
        lambda ctx: (EPOCH, FAR_FUTURE),
    ),
    "item_sales": (
        "SELECT ol_w_id, ol_d_id, count(*) AS lines, sum(ol_quantity) AS quantity, sum(ol_amount) AS amount"
        " FROM ch.order_line WHERE ol_i_id = %s GROUP BY ol_w_id, ol_d_id ORDER BY amount DESC",
        lambda ctx: (random.randint(1, ctx.items),),
    ),
}


def make(name: str):
    sql, params = QUERIES[name]

    async def run(conn: psycopg.AsyncConnection, ctx: TenantCtx) -> None:
        async with conn.transaction():
            cur = await conn.execute(sql, params(ctx))
            await cur.fetchall()

    return run


# Equal weights: each analytical arrival picks one query uniformly.
MIX = [(name, make(name), 1) for name in QUERIES]
