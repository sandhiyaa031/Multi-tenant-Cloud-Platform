"""A throwaway data plane for the twin agent's tests.

The agent manages real PostgreSQL instances, so its tests need one to copy. The
`cluster` fixture builds a miniature of production inside the test container:
a primary with two tenants, a per-tenant partition of ch.order_line, and the
same JSON statement capture. The agent then base-backs it up exactly as it does
the real primary. Nothing here touches the running twin or the data plane.

Environment is set before the agent is imported: its modules read it at import.
"""
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="twintest-"))
PRIMARY, LOGS = TMP / "primary", TMP / "logs"
DELAY_S = 3
os.environ.update(
    TWIN_ROOT=str(TMP / "twin"), TWIN_DELAY_S=str(DELAY_S), TWIN_TOKEN="test-token",
    TWIN_SHARED_BUFFERS="32MB", TWIN_SOURCE_BUFFERS="32MB", DP_LOG_DIR=str(LOGS),
    PRIMARY_HOST="127.0.0.1", REPLICATION_PASSWORD="unused-under-trust",
)
os.environ.pop("TWIN_INSTANCE_CPUS", None)

import psycopg  # noqa: E402
import pytest  # noqa: E402

from agent import pg  # noqa: E402

PRIMARY_PORT = 5432  # the agent's base backup always dials the primary on 5432
TENANTS = {"t_a": (1, 2), "t_b": (3, 4)}
ROWS_PER_TENANT = 20000

SCHEMA = f"""
CREATE ROLE replicator REPLICATION LOGIN;
CREATE ROLE t_a LOGIN;
CREATE ROLE t_b LOGIN;
CREATE EXTENSION hypopg;
CREATE EXTENSION pg_stat_statements;
CREATE SCHEMA ch;
CREATE TABLE ch.tenant_map (db_role text PRIMARY KEY, w_lo int NOT NULL, w_hi int NOT NULL);
INSERT INTO ch.tenant_map VALUES ('t_a', 1, 2), ('t_b', 3, 4);
CREATE TABLE ch.order_line (ol_w_id int NOT NULL, ol_o_id int NOT NULL, ol_i_id int NOT NULL,
                            ol_amount numeric NOT NULL DEFAULT 0, note text) PARTITION BY RANGE (ol_w_id);
CREATE TABLE ch.order_line_t_a PARTITION OF ch.order_line FOR VALUES FROM (1) TO (3);
CREATE TABLE ch.order_line_t_b PARTITION OF ch.order_line FOR VALUES FROM (3) TO (5);
INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id)
    SELECT 1 + n % 2, n, n % 1000 FROM generate_series(1, {ROWS_PER_TENANT}) n;
INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id)
    SELECT 3 + n % 2, n, n % 1000 FROM generate_series(1, {ROWS_PER_TENANT}) n;
GRANT USAGE ON SCHEMA ch TO t_a, t_b;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ch TO t_a, t_b;
ANALYZE;
"""


def sh(*cmd: str) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def primary(dbname: str = "app", user: str = "postgres", **kwargs) -> psycopg.Connection:
    return psycopg.connect(host="127.0.0.1", port=PRIMARY_PORT, dbname=dbname, user=user, autocommit=True, **kwargs)


def wait_until(condition, timeout: float = 30.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def scalar(port: int, sql: str, params=None):
    with pg.connect(port) as conn:
        return conn.execute(sql, params).fetchone()[0]


def note_count(port: int, note: str) -> int:
    return scalar(port, "SELECT count(*) FROM ch.order_line WHERE note = %s", (note,))


def write_marker(note: str) -> str:
    """Commits one recognisable row on the primary; returns the WAL position after it."""
    with primary() as conn:
        conn.execute("INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id, note) VALUES (1, 0, 0, %s)", (note,))
        return conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]


def source_received(lsn: str) -> bool:
    return scalar(pg.SOURCE_PORT, "SELECT pg_last_wal_receive_lsn() >= %s::pg_lsn", (lsn,))


def source_caught_up() -> bool:
    """The source has applied everything production has written."""
    with primary() as conn:
        lsn = conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]
    return scalar(pg.SOURCE_PORT, "SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (lsn,))


def applied_marker(note: str) -> None:
    """A marker that the source has already applied, so its replay timestamp is set."""
    write_marker(note)
    wait_until(lambda: note_count(pg.SOURCE_PORT, note) == 1, what=f"the source to apply {note}")


@pytest.fixture(scope="session")
def cluster():
    LOGS.mkdir(parents=True)
    sh("initdb", "-D", str(PRIMARY), "-U", "postgres", "--auth=trust")
    with open(PRIMARY / "postgresql.conf", "a", encoding="utf-8") as f:
        f.write(
            f"port = {PRIMARY_PORT}\nlisten_addresses = 'localhost'\nshared_buffers = 32MB\nmax_connections = 60\n"
            "wal_level = replica\nmax_wal_senders = 5\nautovacuum = off\nfsync = off\n"
            # The same capture as the production primary in docker-compose.yml.
            f"logging_collector = on\nlog_destination = 'jsonlog'\nlog_directory = '{LOGS}'\n"
            "log_filename = 'pg-%M.log'\nlog_rotation_age = 10min\nlog_truncate_on_rotation = on\n"
            "log_min_duration_statement = 0\nlog_file_mode = 0644\n")
    sh("pg_ctl", "-D", str(PRIMARY), "-w", "-l", str(TMP / "primary.log"), "start")
    try:
        with primary(dbname="postgres") as conn:
            conn.execute("CREATE DATABASE app")
        with primary() as conn:
            conn.execute(SCHEMA)
        pg.ensure_source()
        yield
    finally:
        for datadir in (pg.WORK, pg.SOURCE, PRIMARY):
            subprocess.run(["pg_ctl", "-D", str(datadir), "-w", "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture
def idle_work():
    """Whatever a test does with the working clone, it is stopped afterwards."""
    yield
    pg.stop(pg.WORK)
