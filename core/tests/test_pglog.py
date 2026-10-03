import json

from dbpilot_core.pglog import Assembler, LogTailer, Statement


def rec(pid, ts, message, detail=None, user="t_steady", app="oltp", **extra):
    return {"timestamp": f"2026-10-03 11:00:{ts} UTC", "user": user, "pid": pid, "application_name": app,
            "backend_type": "client backend", "message": message, "detail": detail, **extra}


def test_reassembles_a_transaction_with_parameters():
    a = Assembler()
    assert a.feed(rec(7, "00.010", "duration: 0.050 ms  statement: BEGIN")) is None
    assert a.feed(rec(7, "00.011", "duration: 0.100 ms  parse <unnamed>: UPDATE ch.warehouse SET w_ytd = w_ytd + $1 WHERE w_id = $2")) is None
    assert a.feed(rec(7, "00.012", "duration: 0.200 ms  bind <unnamed>: UPDATE ch.warehouse SET w_ytd = w_ytd + $1 WHERE w_id = $2",
                      "Parameters: $1 = '12.50', $2 = '3'")) is None
    assert a.feed(rec(7, "00.020", "duration: 1.000 ms  execute <unnamed>: UPDATE ch.warehouse SET w_ytd = w_ytd + $1 WHERE w_id = $2",
                      "Parameters: $1 = '12.50', $2 = '3'", query_id=42)) is None
    txn = a.feed(rec(7, "00.030", "duration: 0.500 ms  statement: COMMIT"))

    assert txn.user == "t_steady" and txn.query_class == "OLTP" and not txn.failed
    assert len(txn.statements) == 1
    stmt = txn.statements[0]
    assert stmt.duration_ms == 1.3  # parse + bind + execute
    assert stmt.query_id == 42
    assert stmt.literal_sql() == "UPDATE ch.warehouse SET w_ytd = w_ytd + '12.50' WHERE w_id = '3'"
    # From the start of BEGIN to the end of COMMIT.
    assert round(txn.duration_ms, 2) == 20.05


def test_literal_substitution_handles_quotes_nulls_and_two_digit_placeholders():
    params = {i: f"'v{i}'" for i in range(1, 12)}
    params[2] = "'O''Brien'"
    params[3] = "NULL"
    stmt = Statement("SELECT $1, $2, $3, $10, $11", params, 1.0, None)
    assert stmt.literal_sql() == "SELECT 'v1', 'O''Brien', NULL, 'v10', 'v11'"


def test_interleaved_backends_are_kept_apart():
    a = Assembler()
    a.feed(rec(1, "00.000", "duration: 0.01 ms  statement: BEGIN", user="t_steady"))
    a.feed(rec(2, "00.001", "duration: 0.01 ms  statement: BEGIN", user="t_bursty"))
    a.feed(rec(1, "00.002", "duration: 1.0 ms  execute <unnamed>: SELECT 1"))
    a.feed(rec(2, "00.003", "duration: 1.0 ms  execute <unnamed>: SELECT 2", user="t_bursty"))
    second = a.feed(rec(2, "00.004", "duration: 0.01 ms  statement: COMMIT", user="t_bursty"))
    first = a.feed(rec(1, "00.005", "duration: 0.01 ms  statement: COMMIT"))
    assert (first.user, first.statements[0].sql) == ("t_steady", "SELECT 1")
    assert (second.user, second.statements[0].sql) == ("t_bursty", "SELECT 2")


def test_statement_outside_a_transaction_is_its_own_transaction():
    txn = Assembler().feed(rec(5, "01.000", "duration: 250.0 ms  execute <unnamed>: SELECT count(*) FROM ch.orders", app="olap"))
    assert txn.query_class == "OLAP" and round(txn.duration_ms) == 250


def test_rollback_and_errors_mark_the_transaction_failed():
    a = Assembler()
    a.feed(rec(9, "00.000", "duration: 0.01 ms  statement: BEGIN"))
    assert a.feed(rec(9, "00.001", "duration: 0.01 ms  statement: ROLLBACK")).failed

    a.feed(rec(9, "00.002", "duration: 0.01 ms  statement: BEGIN"))
    a.feed(rec(9, "00.003", "duplicate key value violates unique constraint", error_severity="ERROR"))
    assert a.feed(rec(9, "00.004", "duration: 0.01 ms  statement: ROLLBACK")).failed


def test_non_client_and_non_duration_lines_are_ignored():
    a = Assembler()
    assert a.feed({"backend_type": "checkpointer", "pid": 3, "message": "checkpoint complete"}) is None
    assert a.feed(rec(4, "00.000", "connection authorized: user=t_steady")) is None


def test_tailer_resumes_and_survives_truncation(tmp_path):
    path = tmp_path / "pg-00.json"
    lines = [rec(1, f"00.{i:03d}", f"duration: 1.0 ms  execute <unnamed>: SELECT {i}") for i in range(3)]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines[:2]) + '{"partial":', encoding="utf-8")
    tailer = LogTailer(str(tmp_path))
    assert [t.statements[0].sql for t in tailer.read_new()] == ["SELECT 0", "SELECT 1"]
    assert tailer.read_new() == []  # the partial line is not consumed

    # Rotation reuses the name and truncates the file.
    path.write_text(json.dumps(lines[2]) + "\n", encoding="utf-8")
    assert [t.statements[0].sql for t in tailer.read_new()] == ["SELECT 2"]
