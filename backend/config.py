"""
config.py — Central configuration for the DBPilot workload runner.

Keep all tunables here so the rest of the codebase never hardcodes
connection strings, thresholds, or sampling ranges.

Two separate connection configs are defined on purpose:

  DB_CONFIG      — the READ-ONLY connection used to run EXPLAIN against
                    CartNova's business tables (orders, products, etc.).
                    Should authenticate as a role with SELECT-only grants.

  STORAGE_CONFIG — the WRITE connection used only to persist results
                    into the dbpilot_meta schema. Never touches CartNova's
                    business tables.

Splitting these avoids a real bug: if you set the workload connection's
session to READ ONLY (a safety feature we want) and then try to INSERT
your results on that same connection, Postgres rejects the insert.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DBConfig:
    host: str = os.environ.get("DBPILOT_PG_HOST", "localhost")
    port: int = int(os.environ.get("DBPILOT_PG_PORT", "5432"))
    dbname: str = os.environ.get("DBPILOT_PG_DB", "cartnova")
    user: str = os.environ.get("DBPILOT_PG_USER", "dbpilot_workload_ro")
    password: str = os.environ.get("DBPILOT_PG_PASSWORD", "")

    # Server-side safety net. Any single query running longer than this
    # is killed by Postgres itself, independent of the Python process.
    statement_timeout_ms: int = int(os.environ.get("DBPILOT_STATEMENT_TIMEOUT_MS", "30000"))

    # Reproducibility knobs — see README "PostgreSQL-specific issues".
    disable_jit: bool = True
    disable_parallel_workers: bool = True


@dataclass(frozen=True)
class StorageDBConfig:
    host: str = os.environ.get("DBPILOT_META_PG_HOST", "localhost")
    port: int = int(os.environ.get("DBPILOT_META_PG_PORT", "5432"))
    dbname: str = os.environ.get("DBPILOT_META_PG_DB", "cartnova")
    user: str = os.environ.get("DBPILOT_META_PG_USER", "dbpilot_meta_rw")
    password: str = os.environ.get("DBPILOT_META_PG_PASSWORD", "")


@dataclass(frozen=True)
class RunConfig:
    # How many times each (template, param-sample) pair is executed.
    # Multiple repetitions let downstream ML code use a median/robust
    # runtime instead of a single noisy timing sample.
    repetitions_per_query: int = 3

    # How many distinct parameter samples to draw per template per run.
    samples_per_template: int = 40

    # Fixed seed for reproducibility across collection sessions.
    random_seed: int = 42

    # Known ID ranges in the cartnova dataset. Update if the dataset changes.
    customer_id_range: tuple[int, int] = (1, 100_000)
    product_id_range: tuple[int, int] = (1, 50_000)
    order_id_range: tuple[int, int] = (1, 500_000)

    output_dir: str = os.environ.get("DBPILOT_OUTPUT_DIR", "./workload_output")


DB_CONFIG = DBConfig()
STORAGE_CONFIG = StorageDBConfig()
RUN_CONFIG = RunConfig()
