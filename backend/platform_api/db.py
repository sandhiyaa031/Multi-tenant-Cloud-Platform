"""
platform_api/db.py — Connection handling for the DBPilot PLATFORM
database (dbpilot_platform / schema dbpilot).

This is intentionally a separate module from the existing top-level
db.py, which handles the read-only workload collector's connection to
CartNova. That module and this one connect to two different databases
for two different purposes, and were never meant to share config:

    top-level config.py / db.py   -> cartnova       (workload collector)
    platform_api/db.py (this file) -> dbpilot_platform (this Flask app)

CONNECTION STRATEGY: one psycopg connection is opened per request and
closed at the end of the request (Flask's `g` object + teardown hook).
This is the simplest correct approach for a small student project —
a connection pool (psycopg_pool) is the natural next step once this is
under real concurrent load, but adds a moving part that isn't needed
yet and would just be something else to explain in viva without a
matching need.

CREDENTIALS: read from environment variables only. See .env.example for
the full list. Nothing here is hardcoded.
"""

from __future__ import annotations

import os

import psycopg
from flask import g
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DBPILOT_APP_PG_HOST", "localhost"),
        port=int(os.environ.get("DBPILOT_APP_PG_PORT", "5432")),
        dbname=os.environ.get("DBPILOT_APP_PG_DB", "dbpilot_platform"),
        user=os.environ.get("DBPILOT_APP_PG_USER", "postgres"),
        password=os.environ.get("DBPILOT_APP_PG_PASSWORD", ""),
        options="-c search_path=dbpilot,public",
        row_factory=dict_row,
        autocommit=False,
    )


def get_db() -> psycopg.Connection:
    """
    Returns the request-scoped connection, opening one on first use.
    Flask's `g` object lives for exactly one request, so this naturally
    gives "one connection per request" without any extra bookkeeping.
    """
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(exception: BaseException | None = None) -> None:
    """
    Registered as a Flask teardown handler in __init__.py. Runs after
    every request, success or failure. On failure, roll back first —
    if a route raised partway through a multi-statement operation, we
    must not leave a half-committed transaction sitting on a
    connection that's about to be reused (it won't be reused, it's
    about to close, but rolling back is the correct move regardless of
    what happens to the connection object afterward).
    """
    db = g.pop("db", None)
    if db is not None:
        if exception is not None:
            db.rollback()
        db.close()
