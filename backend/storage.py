"""
storage.py — Persists workload_runner.py results as both:

  1. Rows in the `dbpilot_meta.workload_runs` Postgres table (queryable,
     joinable, the eventual source for ML training data extraction).
  2. A JSONL mirror on disk (dependency-free, easy pandas.read_json,
     survives even if the Postgres table needs to be dropped/rebuilt
     while iterating on the schema).

Storing the full raw EXPLAIN JSON (not just extracted features) is
deliberate: feature engineering will change as the research direction
evolves, and re-parsing stored JSON is free while re-running EXPLAIN
ANALYZE against the real dataset is not.

Everything here writes through STORAGE_CONFIG's connection, which is
separate from the read-only workload connection in db.py — see
config.py for why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection

from plan_parser import PlanFeatures

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS dbpilot_meta;

CREATE TABLE IF NOT EXISTS dbpilot_meta.workload_runs (
    run_id                      BIGSERIAL PRIMARY KEY,
    collection_session_id       UUID NOT NULL,
    template_id                 TEXT NOT NULL,
    template_name               TEXT NOT NULL,
    params                      JSONB NOT NULL,
    sql_text                    TEXT NOT NULL,
    repetition_index            INT NOT NULL,
    executed_at                 TIMESTAMPTZ NOT NULL,
    pg_version                  TEXT NOT NULL,

    planner_startup_cost        DOUBLE PRECISION NOT NULL,
    planner_total_cost          DOUBLE PRECISION NOT NULL,
    planner_estimated_rows      BIGINT NOT NULL,
    actual_rows                 BIGINT NOT NULL,
    actual_loops                INT NOT NULL,
    planning_time_ms            DOUBLE PRECISION NOT NULL,
    execution_time_ms           DOUBLE PRECISION NOT NULL,
    shared_blks_hit              BIGINT NOT NULL,
    shared_blks_read             BIGINT NOT NULL,
    jit_enabled                  BOOLEAN NOT NULL,
    parallel_workers_planned     INT NOT NULL,
    parallel_workers_launched    INT NOT NULL,
    num_joins                    INT NOT NULL,
    num_scans                    INT NOT NULL,
    plan_node_count               INT NOT NULL,
    plan_depth                    INT NOT NULL,
    scan_types                    TEXT[] NOT NULL,
    join_types                    TEXT[] NOT NULL,
    has_seqscan                   BOOLEAN NOT NULL,
    has_index_scan                 BOOLEAN NOT NULL,
    has_bitmap_scan                BOOLEAN NOT NULL,
    has_sort                       BOOLEAN NOT NULL,
    has_hash_agg                   BOOLEAN NOT NULL,
    has_group_agg                  BOOLEAN NOT NULL,
    has_nested_loop                 BOOLEAN NOT NULL,
    has_hash_join                   BOOLEAN NOT NULL,
    has_merge_join                   BOOLEAN NOT NULL,

    raw_plan_json                    JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workload_runs_template_id
    ON dbpilot_meta.workload_runs (template_id);
CREATE INDEX IF NOT EXISTS idx_workload_runs_session
    ON dbpilot_meta.workload_runs (collection_session_id);
"""

_INSERT_SQL = """
INSERT INTO dbpilot_meta.workload_runs (
    collection_session_id, template_id, template_name, params, sql_text,
    repetition_index, executed_at, pg_version,
    planner_startup_cost, planner_total_cost, planner_estimated_rows,
    actual_rows, actual_loops, planning_time_ms, execution_time_ms,
    shared_blks_hit, shared_blks_read, jit_enabled,
    parallel_workers_planned, parallel_workers_launched,
    num_joins, num_scans, plan_node_count, plan_depth,
    scan_types, join_types,
    has_seqscan, has_index_scan, has_bitmap_scan, has_sort,
    has_hash_agg, has_group_agg, has_nested_loop, has_hash_join, has_merge_join,
    raw_plan_json
) VALUES (
    %(collection_session_id)s, %(template_id)s, %(template_name)s, %(params)s, %(sql_text)s,
    %(repetition_index)s, %(executed_at)s, %(pg_version)s,
    %(planner_startup_cost)s, %(planner_total_cost)s, %(planner_estimated_rows)s,
    %(actual_rows)s, %(actual_loops)s, %(planning_time_ms)s, %(execution_time_ms)s,
    %(shared_blks_hit)s, %(shared_blks_read)s, %(jit_enabled)s,
    %(parallel_workers_planned)s, %(parallel_workers_launched)s,
    %(num_joins)s, %(num_scans)s, %(plan_node_count)s, %(plan_depth)s,
    %(scan_types)s, %(join_types)s,
    %(has_seqscan)s, %(has_index_scan)s, %(has_bitmap_scan)s, %(has_sort)s,
    %(has_hash_agg)s, %(has_group_agg)s, %(has_nested_loop)s, %(has_hash_join)s, %(has_merge_join)s,
    %(raw_plan_json)s
)
"""


@dataclass
class WorkloadRunRecord:
    collection_session_id: str
    template_id: str
    template_name: str
    params: dict
    sql_text: str
    repetition_index: int
    executed_at: datetime
    pg_version: str
    features: PlanFeatures
    raw_plan_json: list

    def to_row_params(self) -> dict:
        f = self.features
        return {
            "collection_session_id": self.collection_session_id,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "params": json.dumps(self.params, default=str),
            "sql_text": self.sql_text,
            "repetition_index": self.repetition_index,
            "executed_at": self.executed_at,
            "pg_version": self.pg_version,
            "planner_startup_cost": f.planner_startup_cost,
            "planner_total_cost": f.planner_total_cost,
            "planner_estimated_rows": f.planner_estimated_rows,
            "actual_rows": f.actual_rows,
            "actual_loops": f.actual_loops,
            "planning_time_ms": f.planning_time_ms,
            "execution_time_ms": f.execution_time_ms,
            "shared_blks_hit": f.shared_blks_hit,
            "shared_blks_read": f.shared_blks_read,
            "jit_enabled": f.jit_enabled,
            "parallel_workers_planned": f.parallel_workers_planned,
            "parallel_workers_launched": f.parallel_workers_launched,
            "num_joins": f.num_joins,
            "num_scans": f.num_scans,
            "plan_node_count": f.plan_node_count,
            "plan_depth": f.plan_depth,
            "scan_types": f.scan_types,
            "join_types": f.join_types,
            "has_seqscan": f.has_seqscan,
            "has_index_scan": f.has_index_scan,
            "has_bitmap_scan": f.has_bitmap_scan,
            "has_sort": f.has_sort,
            "has_hash_agg": f.has_hash_agg,
            "has_group_agg": f.has_group_agg,
            "has_nested_loop": f.has_nested_loop,
            "has_hash_join": f.has_hash_join,
            "has_merge_join": f.has_merge_join,
            "raw_plan_json": json.dumps(self.raw_plan_json),
        }

    def to_jsonable(self) -> dict:
        row = self.to_row_params()
        row["executed_at"] = self.executed_at.isoformat()
        row["params"] = self.params
        row["raw_plan_json"] = self.raw_plan_json
        return row


def ensure_schema(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def insert_run(conn: Connection, record: WorkloadRunRecord) -> None:
    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, record.to_row_params())
    conn.commit()
