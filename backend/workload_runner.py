"""
workload_runner.py — Executes DBPilot's controlled CartNova workload and
records both PostgreSQL's planner estimates and actual execution metrics.

Usage:
    python workload_runner.py

Output:
    - Rows inserted into dbpilot_meta.workload_runs (see storage.py)
    - A JSONL mirror written to <output_dir>/<session_id>.jsonl as a
      dependency-free backup / easy pandas.read_json entry point

This script performs no writes to CartNova's business tables. See
db.py for the safety mechanisms enforcing that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row

from config import DB_CONFIG, STORAGE_CONFIG, RUN_CONFIG
from db import (
    get_workload_connection,
    get_storage_connection,
    prepare_workload_session,
    read_only_transaction,
    validate_read_only,
)
from plan_parser import parse_explain_json, PlanFeatures
from query_templates import TEMPLATES, QueryTemplate
from storage import ensure_schema, insert_run, WorkloadRunRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("workload_runner")


def _template_hash(sql: str) -> str:
    """
    Structural hash of the template SQL (not the bound params). This is
    the key used later to split train/test by query template, so the
    model is evaluated on genuinely unseen query shapes rather than
    unseen parameter values of an already-seen shape.
    """
    normalized = " ".join(sql.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _render_sql_for_audit(template: QueryTemplate, params: dict) -> str:
    """
    Produces literal SQL text for the stored sql_text column ONLY — for
    human debugging/audit. Execution always uses parameter binding
    (see run_one), never string formatting.
    """
    rendered = template.sql
    for key, value in params.items():
        literal = f"'{value}'" if not isinstance(value, (int, float)) else str(value)
        rendered = rendered.replace(f"%({key})s", literal)
    return " ".join(rendered.split())


def run_one(
    workload_conn,
    template: QueryTemplate,
    params: dict,
    repetition_index: int,
    session_id: str,
    pg_version: str,
) -> WorkloadRunRecord:
    validate_read_only(template.sql)

    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {template.sql}"

    with read_only_transaction(workload_conn):
        with workload_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(explain_sql, params or None)
            row = cur.fetchone()
            # EXPLAIN (FORMAT JSON) returns a single column named "QUERY PLAN".
            explain_json = row["QUERY PLAN"]

    features: PlanFeatures = parse_explain_json(explain_json)

    return WorkloadRunRecord(
        collection_session_id=session_id,
        template_id=_template_hash(template.sql),
        template_name=template.name,
        params=params,
        sql_text=_render_sql_for_audit(template, params),
        repetition_index=repetition_index,
        executed_at=datetime.now(timezone.utc),
        pg_version=pg_version,
        features=features,
        raw_plan_json=explain_json,
    )


def main() -> None:
    rng = random.Random(RUN_CONFIG.random_seed)
    session_id = str(uuid.uuid4())
    output_dir = Path(RUN_CONFIG.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"{session_id}.jsonl"

    logger.info("Starting collection session %s", session_id)

    with get_storage_connection(STORAGE_CONFIG) as storage_conn:
        ensure_schema(storage_conn)

        with get_workload_connection(DB_CONFIG) as workload_conn:
            prepare_workload_session(workload_conn, DB_CONFIG)

            with workload_conn.cursor() as cur:
                cur.execute("SELECT version()")
                pg_version = cur.fetchone()[0]
            workload_conn.rollback()

            total_planned = (
                len(TEMPLATES)
                * RUN_CONFIG.samples_per_template
                * RUN_CONFIG.repetitions_per_query
            )
            logger.info("Planned executions: %d", total_planned)

            completed, failed = 0, 0
            with jsonl_path.open("w") as jsonl_file:
                for template in TEMPLATES:
                    for sample_idx in range(RUN_CONFIG.samples_per_template):
                        params = template.sample_params(rng, RUN_CONFIG)
                        for rep in range(RUN_CONFIG.repetitions_per_query):
                            try:
                                record = run_one(
                                    workload_conn, template, params, rep, session_id, pg_version
                                )
                            except Exception:
                                failed += 1
                                logger.exception(
                                    "Execution failed: template=%s sample=%d rep=%d",
                                    template.name, sample_idx, rep,
                                )
                                continue

                            insert_run(storage_conn, record)
                            jsonl_file.write(json.dumps(record.to_jsonable(), default=str) + "\n")
                            completed += 1

                            if completed % 50 == 0:
                                logger.info(
                                    "Progress: %d/%d (%d failed)",
                                    completed, total_planned, failed,
                                )

            logger.info(
                "Done. completed=%d failed=%d output=%s", completed, failed, jsonl_path
            )


if __name__ == "__main__":
    main()
