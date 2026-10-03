"""The five TPC-C transactions, confined to one tenant's warehouse range.

Follows the TPC-C specification's transaction profiles and input distributions.
The one deliberate deviation: "remote" warehouses are chosen inside the tenant's
own range, because a tenant cannot (and must not) touch another tenant's rows.
"""
import random
from dataclasses import dataclass

import psycopg

SYLLABLES = ["BAR", "OUGHT", "ABLE", "PRI", "PRES", "ESE", "ANTI", "CALLY", "ATION", "EING"]


@dataclass(frozen=True)
class TenantCtx:
    role: str
    w_lo: int
    w_hi: int
    customers_per_district: int
    items: int

    def warehouse(self) -> int:
        return random.randint(self.w_lo, self.w_hi)

    def other_warehouse(self, w: int) -> int:
        if self.w_lo == self.w_hi:
            return w
        other = random.randint(self.w_lo, self.w_hi - 1)
        return other + 1 if other >= w else other


def nurand(a: int, x: int, y: int) -> int:
    """TPC-C non-uniform random: a skewed distribution, so some rows are hot."""
    return ((random.randint(0, a) | random.randint(x, y)) % (y - x + 1)) + x


def last_name(num: int) -> str:
    return SYLLABLES[num // 100] + SYLLABLES[(num // 10) % 10] + SYLLABLES[num % 10]


async def _customer_by_name(cur, w: int, d: int, name: str) -> int | None:
    """The specification picks the customer in the middle of those sharing a last name."""
    await cur.execute(
        "SELECT c_id FROM ch.customer WHERE c_w_id = %s AND c_d_id = %s AND c_last = %s ORDER BY c_first",
        (w, d, name),
    )
    rows = await cur.fetchall()
    return rows[(len(rows) - 1) // 2][0] if rows else None


async def new_order(conn: psycopg.AsyncConnection, ctx: TenantCtx) -> None:
    w, d = ctx.warehouse(), random.randint(1, 10)
    c = nurand(1023, 1, ctx.customers_per_district)
    # Sorted so that two concurrent orders lock stock rows in the same order and cannot deadlock.
    item_ids = sorted({nurand(8191, 1, ctx.items) for _ in range(random.randint(5, 15))})
    # 1% of orders name an item that does not exist and must roll back.
    invalid_last = random.random() < 0.01
    lines = [(i, ctx.other_warehouse(w) if random.random() < 0.01 else w, random.randint(1, 10)) for i in item_ids]
    all_local = int(all(sw == w for _, sw, _ in lines))

    async with conn.transaction():
        cur = conn.cursor()
        await cur.execute(
            "SELECT c_discount, c_last, c_credit, w_tax FROM ch.customer, ch.warehouse"
            " WHERE w_id = %s AND c_w_id = w_id AND c_d_id = %s AND c_id = %s",
            (w, d, c),
        )
        await cur.fetchone()
        await cur.execute(
            "UPDATE ch.district SET d_next_o_id = d_next_o_id + 1 WHERE d_w_id = %s AND d_id = %s"
            " RETURNING d_next_o_id - 1, d_tax",
            (w, d),
        )
        o_id = (await cur.fetchone())[0]
        await cur.execute(
            "INSERT INTO ch.orders (o_w_id, o_d_id, o_id, o_c_id, o_entry_d, o_ol_cnt, o_all_local)"
            " VALUES (%s, %s, %s, %s, now(), %s, %s)",
            (w, d, o_id, c, len(lines), all_local),
        )
        await cur.execute("INSERT INTO ch.new_order (no_w_id, no_d_id, no_o_id) VALUES (%s, %s, %s)", (w, d, o_id))

        for number, (i_id, supply_w, qty) in enumerate(lines, start=1):
            if invalid_last and number == len(lines):
                i_id = ctx.items + 1
            await cur.execute("SELECT i_price, i_name, i_data FROM ch.item WHERE i_id = %s", (i_id,))
            item = await cur.fetchone()
            if item is None:
                raise psycopg.Rollback()
            await cur.execute(
                f"UPDATE ch.stock SET"
                f" s_quantity = CASE WHEN s_quantity >= %s + 10 THEN s_quantity - %s ELSE s_quantity - %s + 91 END,"
                f" s_ytd = s_ytd + %s, s_order_cnt = s_order_cnt + 1, s_remote_cnt = s_remote_cnt + %s"
                f" WHERE s_w_id = %s AND s_i_id = %s RETURNING s_dist_{d:02d}, s_data",
                (qty, qty, qty, qty, int(supply_w != w), supply_w, i_id),
            )
            dist_info = (await cur.fetchone())[0]
            await cur.execute(
                "INSERT INTO ch.order_line (ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_supply_w_id,"
                " ol_quantity, ol_amount, ol_dist_info) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (w, d, o_id, number, i_id, supply_w, qty, qty * item[0], dist_info),
            )


async def payment(conn: psycopg.AsyncConnection, ctx: TenantCtx) -> None:
    w, d = ctx.warehouse(), random.randint(1, 10)
    amount = random.randint(100, 500000) / 100
    if random.random() < 0.85:
        c_w, c_d = w, d
    else:
        c_w, c_d = ctx.other_warehouse(w), random.randint(1, 10)

    async with conn.transaction():
        cur = conn.cursor()
        await cur.execute("UPDATE ch.warehouse SET w_ytd = w_ytd + %s WHERE w_id = %s RETURNING w_name", (amount, w))
        w_name = (await cur.fetchone())[0]
        await cur.execute(
            "UPDATE ch.district SET d_ytd = d_ytd + %s WHERE d_w_id = %s AND d_id = %s RETURNING d_name",
            (amount, w, d),
        )
        d_name = (await cur.fetchone())[0]

        c = None
        if random.random() < 0.6:
            c = await _customer_by_name(cur, c_w, c_d, last_name(nurand(255, 0, 999)))
        if c is None:
            c = nurand(1023, 1, ctx.customers_per_district)

        await cur.execute(
            "UPDATE ch.customer SET c_balance = c_balance - %s, c_ytd_payment = c_ytd_payment + %s,"
            " c_payment_cnt = c_payment_cnt + 1 WHERE c_w_id = %s AND c_d_id = %s AND c_id = %s RETURNING c_credit",
            (amount, amount, c_w, c_d, c),
        )
        if (await cur.fetchone())[0] == "BC":
            await cur.execute(
                "UPDATE ch.customer SET c_data = left(%s || c_data, 500)"
                " WHERE c_w_id = %s AND c_d_id = %s AND c_id = %s",
                (f"{c} {c_d} {c_w} {d} {w} {amount:.2f} ", c_w, c_d, c),
            )
        await cur.execute(
            "INSERT INTO ch.history (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_w_id, h_date, h_amount, h_data)"
            " VALUES (%s, %s, %s, %s, %s, now(), %s, %s)",
            (c, c_d, c_w, d, w, amount, f"{w_name}    {d_name}"[:24]),
        )


async def order_status(conn: psycopg.AsyncConnection, ctx: TenantCtx) -> None:
    w, d = ctx.warehouse(), random.randint(1, 10)
    async with conn.transaction():
        cur = conn.cursor()
        c = None
        if random.random() < 0.6:
            c = await _customer_by_name(cur, w, d, last_name(nurand(255, 0, 999)))
        if c is None:
            c = nurand(1023, 1, ctx.customers_per_district)
        await cur.execute(
            "SELECT c_balance, c_first, c_middle, c_last FROM ch.customer"
            " WHERE c_w_id = %s AND c_d_id = %s AND c_id = %s",
            (w, d, c),
        )
        await cur.fetchone()
        await cur.execute(
            "SELECT o_id, o_entry_d, o_carrier_id FROM ch.orders"
            " WHERE o_w_id = %s AND o_d_id = %s AND o_c_id = %s ORDER BY o_id DESC LIMIT 1",
            (w, d, c),
        )
        order = await cur.fetchone()
        if order:
            await cur.execute(
                "SELECT ol_i_id, ol_supply_w_id, ol_quantity, ol_amount, ol_delivery_d FROM ch.order_line"
                " WHERE ol_w_id = %s AND ol_d_id = %s AND ol_o_id = %s",
                (w, d, order[0]),
            )
            await cur.fetchall()


async def delivery(conn: psycopg.AsyncConnection, ctx: TenantCtx) -> None:
    w, carrier = ctx.warehouse(), random.randint(1, 10)
    async with conn.transaction():
        cur = conn.cursor()
        for d in range(1, 11):
            # SKIP LOCKED: two concurrent deliveries take different orders instead of queueing on one.
            await cur.execute(
                "DELETE FROM ch.new_order WHERE no_w_id = %s AND no_d_id = %s AND no_o_id = ("
                "   SELECT no_o_id FROM ch.new_order WHERE no_w_id = %s AND no_d_id = %s"
                "   ORDER BY no_o_id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING no_o_id",
                (w, d, w, d),
            )
            row = await cur.fetchone()
            if row is None:
                continue
            o_id = row[0]
            await cur.execute(
                "UPDATE ch.orders SET o_carrier_id = %s WHERE o_w_id = %s AND o_d_id = %s AND o_id = %s"
                " RETURNING o_c_id",
                (carrier, w, d, o_id),
            )
            c = (await cur.fetchone())[0]
            await cur.execute(
                "WITH delivered AS (UPDATE ch.order_line SET ol_delivery_d = now()"
                "   WHERE ol_w_id = %s AND ol_d_id = %s AND ol_o_id = %s RETURNING ol_amount)"
                " SELECT coalesce(sum(ol_amount), 0) FROM delivered",
                (w, d, o_id),
            )
            total = (await cur.fetchone())[0]
            await cur.execute(
                "UPDATE ch.customer SET c_balance = c_balance + %s, c_delivery_cnt = c_delivery_cnt + 1"
                " WHERE c_w_id = %s AND c_d_id = %s AND c_id = %s",
                (total, w, d, c),
            )


async def stock_level(conn: psycopg.AsyncConnection, ctx: TenantCtx) -> None:
    w, d, threshold = ctx.warehouse(), random.randint(1, 10), random.randint(10, 20)
    async with conn.transaction():
        cur = conn.cursor()
        await cur.execute("SELECT d_next_o_id FROM ch.district WHERE d_w_id = %s AND d_id = %s", (w, d))
        next_o_id = (await cur.fetchone())[0]
        await cur.execute(
            "SELECT count(DISTINCT s_i_id) FROM ch.order_line, ch.stock"
            " WHERE ol_w_id = %s AND ol_d_id = %s AND ol_o_id < %s AND ol_o_id >= %s"
            " AND s_w_id = %s AND s_i_id = ol_i_id AND s_quantity < %s",
            (w, d, next_o_id, next_o_id - 20, w, threshold),
        )
        await cur.fetchone()


# (name, function, weight): the specification's transaction mix.
MIX = [
    ("new_order", new_order, 45),
    ("payment", payment, 43),
    ("order_status", order_status, 4),
    ("delivery", delivery, 4),
    ("stock_level", stock_level, 4),
]
