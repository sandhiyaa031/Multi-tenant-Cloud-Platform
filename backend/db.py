"""
db.py — Connection handling and the read-only execution safety net.

Three independent layers of defense are used here, on purpose:

  1. A dedicated Postgres role with SELECT-only grants (see schema.sql).
     This is the layer that actually matters — even if every line of
     Python below has a bug, this role physically cannot DROP a table.
  2. A Python-side statement validator that rejects anything that isn't
     a single, simple SELECT/WITH statement.
  3. A transaction that is ALWAYS rolled back, never committed, plus a
     server-side statement_timeout that doesn't depend on this process
     being alive.

No single layer is trusted alone. This matters because workload_runner.py
runs templated SQL repeatedly and unattended against a real database —
"we wrote the templates ourselves" is not a safety argument to rely on.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection

from config import DBConfig, StorageDBConfig

logger = logging.getLogger(__name__)

# Defense-in-depth, not the primary control — the primary control is
# that the workload DB role itself has no write privileges.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|REINDEX|EXECUTE|CALL|DO|LOCK|MERGE|REFRESH"
    r")\b",
    re.IGNORECASE,
)

_ALLOWED_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


class UnsafeQueryError(ValueError):
    """Raised when a query fails the read-only safety check."""


def validate_read_only(sql: str) -> None:
    """
    Reject anything that isn't a single, simple SELECT/WITH statement.

    This intentionally rejects some legitimate-but-complex SQL (e.g. a
    SELECT containing the literal word "CREATE" inside a string literal).
    For a workload runner driven by our own hand-written templates, that
    false-positive rate is an acceptable price for a simple, auditable
    check — do not "fix" this by writing a real SQL parser here.
    """
    stripped = sql.strip()

    if not _ALLOWED_START.match(stripped):
        raise UnsafeQueryError(
            f"Query must start with SELECT or WITH. Got: {stripped[:60]!r}"
        )

    # Allow exactly one optional trailing semicolon; reject anything else
    # containing a semicolon (i.e. multiple statements).
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        raise UnsafeQueryError("Multiple statements are not allowed.")

    if _FORBIDDEN_KEYWORDS.search(body):
        raise UnsafeQueryError(
            "Query contains a forbidden keyword (write/DDL/admin statement)."
        )


@contextmanager
def get_workload_connection(cfg: DBConfig) -> Iterator[Connection]:
    """
    Yields a psycopg3 connection configured for safe, reproducible
    read-only EXPLAIN ANALYZE collection against CartNova.

    prepare_threshold=None disables psycopg3's automatic server-side
    prepared statements. This matters: PostgreSQL switches a prepared
    statement from a "custom plan" (built using the actual bound values,
    accurate row estimates) to a cached "generic plan" (built from
    average statistics) after the 5th execution by default. If we let
    that happen, cost estimates for the same query template silently
    change partway through collection for reasons unrelated to the query.
    """
    conn = psycopg.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        password=cfg.password,
        autocommit=False,
        prepare_threshold=None,
    )
    conn.prepare_threshold = None  # belt-and-suspenders across psycopg versions
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_storage_connection(cfg: StorageDBConfig) -> Iterator[Connection]:
    """
    Yields a plain writable connection used ONLY for persisting results
    into the dbpilot_meta schema. Never used to touch CartNova's
    business tables.
    """
    conn = psycopg.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        password=cfg.password,
        autocommit=False,
    )
    try:
        yield conn
    finally:
        conn.close()


def prepare_workload_session(conn: Connection, cfg: DBConfig) -> None:
    """
    Apply session-level settings once, before any workload queries run.
    These are reproducibility controls, not safety controls — the
    safety controls are the read-only role, validate_read_only(), and
    the always-rollback pattern in read_only_transaction() below.
    """
    with conn.cursor() as cur:
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cur.execute(f"SET statement_timeout = {cfg.statement_timeout_ms}")
        #cur.execute("SET track_io_timing = on")  # required for BUFFERS I/O timing
        if cfg.disable_jit:
            cur.execute("SET jit = off")
        if cfg.disable_parallel_workers:
            cur.execute("SET max_parallel_workers_per_gather = 0")
    conn.commit()  # commits the SET statements themselves, not any workload query


@contextmanager
def read_only_transaction(conn: Connection) -> Iterator[None]:
    """
    Wraps a single workload query execution. ALWAYS rolls back, even on
    success, so an EXPLAIN ANALYZE-executed query can never leave a side
    effect, regardless of what the query turns out to do.
    """
    try:
        yield
    finally:
        conn.rollback()
