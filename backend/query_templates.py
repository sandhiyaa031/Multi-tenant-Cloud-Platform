from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from config import RunConfig

ParamSampler = Callable[[random.Random, RunConfig], dict]


@dataclass(frozen=True)
class QueryTemplate:
    name: str
    sql: str
    sample_params: ParamSampler


def _sample_customer_point_lookup(rng: random.Random, cfg: RunConfig) -> dict:
    return {"customer_id": rng.randint(*cfg.customer_id_range)}


def _sample_category(rng: random.Random, cfg: RunConfig) -> dict:
    categories = [
        "Electronics",
        "Fashion",
        "Home",
        "Books",
        "Sports",
        "Beauty",
        "Toys",
        "Grocery",
    ]
    return {"category": rng.choice(categories)}

def _sample_date_range(rng: random.Random, cfg: RunConfig) -> dict:
    start = date(2025, 1, 1) + timedelta(days=rng.randint(0, 500))
    end = start + timedelta(days=rng.randint(1, 30))
    return {"start_date": start, "end_date": end}


def _sample_order_status(rng: random.Random, cfg: RunConfig) -> dict:
    return {"status": rng.choice(["pending", "shipped", "delivered", "cancelled"])}

def _sample_payment_status(rng: random.Random, cfg: RunConfig) -> dict:
    return {
        "payment_status": rng.choice([
            "PENDING",
            "SUCCESS",
            "FAILED",
        ])
    }


def _sample_order_count_threshold(rng: random.Random, cfg: RunConfig) -> dict:
    return {"threshold": rng.randint(2, 15)}


def _sample_status_and_date(rng: random.Random, cfg: RunConfig) -> dict:
    return {
        "status": _sample_order_status(rng, cfg)["status"],
        "start_date": _sample_date_range(rng, cfg)["start_date"],
    }


TEMPLATES: list[QueryTemplate] = [
    QueryTemplate(
        name="point_lookup_customer_orders",
        sql="""
            SELECT order_id, order_date, status
            FROM orders
            WHERE customer_id = %(customer_id)s
        """,
        sample_params=_sample_customer_point_lookup,
    ),
    QueryTemplate(
        name="product_category_scan",
        sql="""
            SELECT product_id, price
            FROM products
            WHERE category = %(category)s
        """,
        sample_params=_sample_category,
    ),
    QueryTemplate(
        name="order_items_join_products",
        sql="""
            SELECT o.order_id, p.product_id, oi.quantity
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.order_id
            JOIN products p ON p.product_id = oi.product_id
            WHERE o.customer_id = %(customer_id)s
        """,
        sample_params=_sample_customer_point_lookup,
    ),
    QueryTemplate(
        name="heavy_multi_join_analytics",
        sql="""
            SELECT c.customer_id, SUM(oi.quantity * oi.unit_price) AS total_spend
            FROM customers c
            JOIN orders o ON o.customer_id = c.customer_id
            JOIN order_items oi ON oi.order_id = o.order_id
            JOIN payments pay ON pay.order_id = o.order_id
            WHERE o.order_date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY c.customer_id
            ORDER BY total_spend DESC
            LIMIT 100
        """,
        sample_params=_sample_date_range,
    ),
    QueryTemplate(
        name="order_status_date_filter",
        sql="""
            SELECT order_id, customer_id, order_date
            FROM orders
            WHERE status = %(status)s
              AND order_date > %(start_date)s
        """,
        sample_params=_sample_status_and_date,
    ),
    QueryTemplate(
        name="category_aggregate_full_scan",
        sql="""
            SELECT category, COUNT(*) AS n, AVG(price) AS avg_price
            FROM products
            GROUP BY category
        """,
        sample_params=lambda rng, cfg: {},
    ),

    QueryTemplate(
    name="payments_status_join",
    sql="""
        SELECT o.order_id, pay.amount
        FROM orders o
        JOIN payments pay ON pay.order_id = o.order_id
        WHERE pay.payment_status = %(payment_status)s
    """,
    sample_params=_sample_payment_status,
),
    QueryTemplate(
        name="frequent_customers_subquery",
        sql="""
            SELECT customer_id
            FROM (
                SELECT customer_id, COUNT(*) AS order_count
                FROM orders
                GROUP BY customer_id
                HAVING COUNT(*) > %(threshold)s
            ) frequent
        """,
        sample_params=_sample_order_count_threshold,
    ),
]
