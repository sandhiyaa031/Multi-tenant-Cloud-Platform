"""Tests of the benchmark arithmetic. Run in the bench-tools container:

    docker compose ... run --rm bench-tools -m pytest -q -p no:cacheprovider /tools/bench
"""
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402
from dbpilot_core.pglog import Statement, Transaction  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
E0 = T0.timestamp()


def txn(role, app, at, statements, span_ms):
    start = T0 + timedelta(seconds=at)
    return Transaction(user=role, app=app, pid=1, start=start, end=start + timedelta(milliseconds=span_ms),
                       statements=[Statement(sql, {}, ms, qid) for sql, ms, qid in statements])


def test_client_metrics_are_per_class_and_per_operation_inside_the_window_only():
    ops = [[E0 + i, "t_a", "OLTP", "new_order", 10.0 + i] for i in range(10)]
    ops += [[E0 + i, "t_a", "OLTP", "payment", 5.0] for i in range(10)]
    ops += [[E0 + 50, "t_a", "OLTP", "payment", 999.0]]                     # outside the window
    got = metrics.client_metrics(ops, E0, E0 + 10)
    assert got["t_a/OLTP"]["n"] == 20 and got["t_a/OLTP"]["per_s"] == 2.0
    assert got["t_a/OLTP/new_order"] == {"n": 10, "p50": 14.0, "p95": 19.0, "p99": 19.0, "mean": 14.5, "per_s": 1.0}
    assert got["t_a/OLTP/payment"]["p99"] == 5.0


def test_server_metrics_give_one_summary_per_query_template():
    transactions = [txn("t_a", "oltp", i, [("SELECT c_discount FROM x", 2.0, 11), ("INSERT INTO y", 1.0 + i, 22)], 9.0)
                    for i in range(5)]
    got, texts = metrics.server_metrics(transactions, E0, E0 + 60)
    assert got["t_a/OLTP"]["n"] == 5 and got["t_a/OLTP"]["p50"] == 9.0             # the in-database span
    assert got["t_a/OLTP#11"]["p95"] == 2.0 and got["t_a/OLTP#22"]["p95"] == 5.0   # one template each
    assert texts == {"11": "SELECT c_discount FROM x", "22": "INSERT INTO y"}


def test_db_time_share_is_execution_time_over_what_the_client_saw():
    # 4 ms executing, 10 ms inside the database from first statement to commit, 20 ms at the client.
    transactions = [txn("t_a", "oltp", i, [("SELECT c_discount FROM x", 3.0, 1), ("INSERT INTO y", 1.0, 2)], 10.0) for i in range(4)]
    transactions.append(txn("t_a", "olap", 1, [("SELECT sum(x) FROM z", 80.0, 3)], 80.0))
    ops = [[E0 + i, "t_a", "OLTP", "new_order", 20.0] for i in range(4)] + [[E0 + 1, "t_a", "OLAP", "q1", 100.0]]
    got = metrics.db_time_share(ops, transactions, E0, E0 + 60)
    row = got["t_a/OLTP/new_order"]
    assert row["db_time_share"] == 0.2 and row["in_db_span_share"] == 0.5 and row["statements_per_txn"] == 2
    assert got["t_a/OLTP"]["db_time_share"] == 0.2
    assert got["t_a/OLAP/olap"]["db_time_share"] == 0.8
    # The monitoring role's statements are in the same log and are no part of the workload.
    transactions.append(txn("dbpilot_monitor", "oltp", 2, [("SELECT 1", 1.0, 9)], 1.0))
    assert set(metrics.db_time_share(ops, transactions, E0, E0 + 60)) == set(got)
    assert "dbpilot_monitor/OLTP" not in metrics.server_metrics(transactions, E0, E0 + 60, {"t_a"})[0]


def test_logged_transactions_are_typed_by_their_first_statement():
    assert metrics.transaction_type(txn("t_a", "oltp", 0, [("UPDATE ch.warehouse SET w_ytd = 1", 1, 1)], 1)) == "payment"
    assert metrics.transaction_type(txn("t_a", "oltp", 0, [("DELETE FROM ch.new_order WHERE 1=1", 1, 1)], 1)) == "delivery"
    assert metrics.transaction_type(txn("t_a", "olap", 0, [("SELECT 1", 1, 1)], 1)) == "olap"
    assert metrics.transaction_type(txn("t_a", "oltp", 0, [("VACUUM", 1, 1)], 1)) == "other"


def test_noise_reports_the_spread_and_the_effect_ten_repetitions_can_detect():
    rng = random.Random(4)
    ratios = [rng.gauss(0.0, 0.05) for _ in range(12)]
    got = metrics.noise(ratios)
    sd = got["sd_log"]
    assert got["n"] == 12 and 0.03 < sd < 0.08
    assert got["sd_log_lo"] < sd < got["sd_log_hi"]
    # MDE10 = 0.995 * s on the log scale (docs/evidence/bench_noise/protocol.md).
    assert math.isclose(math.log(1 + got["mde10_pct"] / 100), 0.9947 * sd, rel_tol=1e-3)
    assert got["mean_lo"] < got["mean_ratio"] < got["mean_hi"]
    assert metrics.noise([0.01, 0.02]) is None                              # too few to speak of a spread


def test_identical_repetitions_have_no_noise():
    got = metrics.noise([0.0] * 12)
    assert got["sd_log"] == 0 and got["mde10_pct"] == 0 and got["mean_ratio"] == 1
