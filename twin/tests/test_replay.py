"""Replay of captured transactions against a clone, and one whole twin run.

The replay tests hand the replayer transactions built by hand, so what must come
out is known exactly. The run tests go through the real capture: statements are
issued on the test primary as tenants, read back from its JSON log, and replayed.
"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from agent import pg, runner
from agent.replay import replay
from dbpilot_core.pglog import Statement, Transaction

from .conftest import PRIMARY_PORT, applied_marker, note_count, primary, scalar, wait_until

pytestmark = pytest.mark.usefixtures("cluster", "idle_work")

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
INDEX_FOR_T_A = {"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_a"}


def txn(role: str, sql: list[str], offset_s: float = 0.0, app: str = "oltp", failed: bool = False) -> Transaction:
    start = T0 + timedelta(seconds=offset_s)
    return Transaction(user=role, app=app, pid=1, start=start, end=start, failed=failed,
                       statements=[Statement(s, {}, 1.0, None) for s in sql])


def insert(note: str, w_id: int = 1) -> str:
    return f"INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id, note) VALUES ({w_id}, 0, 0, '{note}')"


@pytest.fixture
def clone():
    applied_marker(f"replay-base-{uuid.uuid4().hex[:8]}")
    pg.freeze_base()
    pg.fresh_work()


def run_replay(transactions, limits=None):
    return asyncio.run(replay(transactions, T0, pg.WORK_PORT, limits or {}))


def test_replay_runs_each_transaction_as_its_tenant_and_reports_per_tenant_and_class(clone):
    transactions = [txn("t_a", [insert("a1"), insert("a2")]), txn("t_a", [insert("a3")], offset_s=0.2),
                    txn("t_b", ["SELECT count(*) FROM ch.order_line"], app="olap", offset_s=0.1),
                    txn("t_b", [insert("b1", w_id=3)], offset_s=0.3)]
    samples, errors, examples = run_replay(transactions)

    assert {key: len(values) for key, values in samples.items()} == {"t_a/OLTP": 2, "t_b/OLAP": 1, "t_b/OLTP": 1}
    assert errors == {} and examples == {}
    # Each sample is (scheduled offset from T0, latency in ms).
    assert sorted(offset for offset, _ in samples["t_a/OLTP"]) == [0.0, 0.2]
    assert all(latency > 0 for values in samples.values() for _, latency in values)
    for note in ("a1", "a2", "a3", "b1"):
        assert note_count(pg.WORK_PORT, note) == 1
    # Executed by the tenant's own role, not by a superuser.
    assert scalar(pg.WORK_PORT, "SELECT count(*) FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid"
                                " WHERE r.rolname = 't_a' AND s.query ILIKE 'INSERT INTO ch.order_line%'") >= 1
    # Production saw none of it.
    assert note_count(PRIMARY_PORT, "a1") == 0


def test_replay_starts_transactions_at_their_original_offsets(clone):
    transactions = [txn("t_a", [insert("early")]), txn("t_a", [insert("late")], offset_s=1.5)]
    before = time.monotonic()
    samples, _, _ = run_replay(transactions)
    # The last transaction was not launched before its offset, however fast the first one finished.
    assert time.monotonic() - before >= 1.5
    assert sorted(offset for offset, _ in samples["t_a/OLTP"]) == [0.0, 1.5]


def test_transaction_that_rolled_back_in_production_rolls_back_on_the_clone(clone):
    samples, errors, _ = run_replay([txn("t_a", [insert("rolled-back")], failed=True)])
    # It did the same work and is measured, but leaves nothing behind.
    assert len(samples["t_a/OLTP"]) == 1 and errors == {}
    assert note_count(pg.WORK_PORT, "rolled-back") == 0


def test_statement_that_fails_on_the_clone_is_counted_not_measured_and_does_not_stop_the_replay(clone):
    transactions = [txn("t_a", [insert("partial"), "SELECT * FROM ch.no_such_table"]),
                    txn("t_a", [insert("after-the-error")], offset_s=0.2)]
    samples, errors, examples = run_replay(transactions)
    assert errors == {"t_a/OLTP": 1}
    assert "UndefinedTable" in examples["t_a/OLTP"]
    assert len(samples["t_a/OLTP"]) == 1
    # The failed transaction is atomic: its first statement is undone as well.
    assert note_count(pg.WORK_PORT, "partial") == 0
    assert note_count(pg.WORK_PORT, "after-the-error") == 1


def test_tenant_cannot_do_on_the_clone_what_it_could_not_do_in_production(clone):
    _, errors, examples = run_replay([txn("t_a", ["DROP TABLE ch.order_line_t_b"])])
    assert errors == {"t_a/OLTP": 1} and "InsufficientPrivilege" in examples["t_a/OLTP"]


def test_concurrency_cap_makes_excess_transactions_queue_and_the_wait_is_measured(clone):
    with pg.connect(pg.WORK_PORT) as conn:
        conn.execute("ALTER ROLE t_a CONNECTION LIMIT 2")
    transactions = [txn("t_a", ["SELECT pg_sleep(0.3)"]) for _ in range(8)]
    samples, errors, _ = run_replay(transactions, {"t_a": 2})

    # Nothing is refused for exceeding the limit: the excess waits for a connection...
    assert errors == {} and len(samples["t_a/OLTP"]) == 8
    latencies = sorted(latency for _, latency in samples["t_a/OLTP"])
    # ...and that wait is part of its latency: eight 0.3 s transactions, two at a time.
    assert latencies[0] < 600 and latencies[-1] >= 1100


def test_replay_of_nothing_returns_nothing(clone):
    assert run_replay([]) == ({}, {}, {})


# ── One whole run, through the real statement capture ────────────────────────

def tenant_workload(tag: str) -> int:
    """Issues a small, known workload on the primary as the two tenants. Returns
    how many transactions it was."""
    count = 0
    for role, w_id, app in (("t_a", 1, "oltp"), ("t_b", 3, "oltp")):
        with primary(user=role, application_name=app) as conn:
            for n in range(5):
                with conn.transaction():
                    conn.execute("INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id, note) VALUES (%s, %s, %s, %s)",
                                 (w_id, n, n, f"{tag}-{role}-{n}"))
                count += 1
    with primary(user="t_a", application_name="olap") as conn:
        for n in range(3):
            conn.execute("SELECT count(*) FROM ch.order_line WHERE ol_i_id = %s", (n,))
            count += 1
    with primary() as conn:  # not a tenant: must not be replayed
        conn.execute("SELECT count(*) FROM ch.tenant_map")
    return count


def total(samples: dict) -> int:
    return sum(len(values) for values in samples.values())


def test_run_replays_the_captured_window_on_control_and_treatment():
    applied_marker("run-t0")
    expected = tenant_workload("run")
    result = runner.run(INDEX_FOR_T_A, window_s=60)

    assert result["transactions"] == expected and result["repetitions"] == 1
    assert 0 < result["window_s"] <= 60
    control, treatment = result["arms"]["control"], result["arms"]["treatment"]
    index = "dbp_order_line_"
    # The action is applied to the treatment clone only, by the same executor as production.
    assert "applied" not in control and control["storage_delta_bytes"] == 0
    assert len(treatment["applied"]) == 1 and "ch.order_line_t_a (ol_i_id)" in treatment["applied"][0]
    assert treatment["inverse"][0].startswith("DROP INDEX") and treatment["storage_delta_bytes"] > 0
    # Both arms replayed the same transactions, per tenant and class, without error.
    for arm in (control, treatment):
        assert {key: len(values) for key, values in arm["samples"].items()} == \
            {"t_a/OLTP": 5, "t_a/OLAP": 3, "t_b/OLTP": 5}
        assert arm["errors"] == {} and arm["wal_bytes"] > 0

    # Afterwards: no clone left running, production untouched, the source following again.
    assert not pg.is_running(pg.WORK)
    assert scalar(PRIMARY_PORT, "SELECT count(*) FROM pg_indexes WHERE indexname LIKE %s", (index + "%",)) == 0
    assert scalar(PRIMARY_PORT, "SELECT count(*) FROM ch.order_line WHERE note LIKE 'run-%%'") == 10 + 1
    assert pg.source_status()["pause_state"] == "not paused"
    wait_until(lambda: note_count(pg.SOURCE_PORT, "run-t_b-4") == 1, what="the source to keep replaying")


def test_replayed_transaction_is_not_already_on_the_clone():
    """Regression for the inclusive recovery target: the first transaction after T0
    used to be both on the clone and in the replay, so it ran twice."""
    applied_marker("once-t0")
    tenant_workload("once")
    result = runner.run(None, window_s=60)
    assert result["arms"]["control"]["errors"] == {}
    # The last arm's clone is still on disk; start it to look inside.
    pg.start(pg.WORK, pg.WORK_PORT)
    assert scalar(pg.WORK_PORT, "SELECT count(*) FROM ch.order_line WHERE note = 'once-t_a-0'") == 1


def test_run_without_an_action_is_an_a_a_test_and_repetitions_accumulate():
    applied_marker("aa-t0")
    expected = tenant_workload("aa")
    result = runner.run(None, window_s=60, repetitions=2)
    assert result["action"] is None and result["repetitions"] == 2
    for arm in result["arms"].values():
        assert "applied" not in arm
        assert total(arm["samples"]) == expected * 2


def test_run_refuses_an_action_the_twin_cannot_evaluate_before_touching_anything():
    base_before = (pg.BASE / "postgresql.auto.conf").stat().st_mtime_ns if pg.BASE.exists() else None
    with pytest.raises(ValueError, match="cannot be evaluated on the twin"):
        runner.run({"type": "replica_routing", "tenant_role": "t_a", "enabled": True}, window_s=60)
    with pytest.raises(ValueError):
        runner.run({"type": "run_sql", "sql": "DROP TABLE ch.order_line"}, window_s=60)
    assert pg.source_status()["pause_state"] == "not paused"
    assert ((pg.BASE / "postgresql.auto.conf").stat().st_mtime_ns if pg.BASE.exists() else None) == base_before


def test_run_for_an_unknown_tenant_fails_and_leaves_the_node_usable():
    applied_marker("unknown-tenant")
    with pytest.raises(Exception, match="not a tenant of this cluster"):
        runner.run({**INDEX_FOR_T_A, "tenant_role": "t_nobody"}, window_s=60)
    # The source is running and following; the next run works.
    assert pg.is_running(pg.SOURCE) and pg.source_status()["pause_state"] == "not paused"
    applied_marker("unknown-tenant-next")
    assert runner.run(None, window_s=60)["arms"].keys() == {"control", "treatment"}


def test_window_never_reaches_past_the_present():
    """Asked for more workload than has been captured since T0, the run takes what exists."""
    applied_marker("window")
    result = runner.run(None, window_s=1800)
    assert result["window_s"] < 60
    assert datetime.fromisoformat(result["t1"]) <= datetime.now(timezone.utc)
