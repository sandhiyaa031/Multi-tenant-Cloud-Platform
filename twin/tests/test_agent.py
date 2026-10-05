"""The agent's HTTP surface and the orchestration of a run, with the database
work replaced by stand-ins: these tests start no PostgreSQL instance."""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent import app as agent_app
from agent import pg, runner
from dbpilot_core.pglog import Statement, Transaction

AUTH = {"Authorization": "Bearer test-token"}
T0 = datetime.now(timezone.utc) - timedelta(seconds=30)


@pytest.fixture
def client(monkeypatch):
    # No `with`: the lifespan, which would start the real source, does not run.
    monkeypatch.setattr(agent_app, "runs", {})
    stopped = []
    monkeypatch.setattr(pg, "stop", lambda datadir: stopped.append(datadir))
    client = TestClient(agent_app.app)
    client.stopped = stopped
    yield client
    # A run's thread releases the node just after its state is readable; let it finish.
    deadline = time.monotonic() + 10
    while agent_app.busy.locked() and time.monotonic() < deadline:
        time.sleep(0.02)


def wait_for(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.get(f"/runs/{run_id}", headers=AUTH).json()
        if state["state"] != "RUNNING":
            return state
        time.sleep(0.02)
    raise AssertionError("run did not finish")


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "test-token"}])
def test_every_endpoint_requires_the_token(client, headers):
    assert client.get("/status", headers=headers).status_code == 401
    assert client.post("/runs", headers=headers, json={}).status_code == 401
    assert client.get("/runs/abc", headers=headers).status_code == 401
    assert client.post("/whatif", headers=headers, json={"action": {}, "queries": []}).status_code == 401
    assert client.post("/explain", headers=headers, json={"query": "SELECT 1"}).status_code == 401


def test_run_request_is_bounded(client):
    for body in ({"window_s": 5}, {"window_s": 5000}, {"repetitions": 0}, {"repetitions": 6}):
        assert client.post("/runs", headers=AUTH, json=body).status_code == 422
    assert not agent_app.busy.locked()


def test_run_result_is_returned_and_the_node_is_free_again(client, monkeypatch):
    seen = {}

    def fake_run(action, window_s, repetitions, treatment_first):
        seen.update(action=action, window_s=window_s, repetitions=repetitions, treatment_first=treatment_first)
        return {"arms": {}}

    monkeypatch.setattr(runner, "run", fake_run)
    body = {"action": {"type": "analyze", "table": "orders"}, "window_s": 55, "repetitions": 2, "treatment_first": True}
    r = client.post("/runs", headers=AUTH, json=body)
    assert r.status_code == 202
    assert wait_for(client, r.json()["run_id"]) == {"state": "DONE", "result": {"arms": {}}}
    assert seen == body
    assert not agent_app.busy.locked()


def test_only_one_run_at_a_time(client, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(runner, "run", lambda *a: release.wait(10) and {"arms": {}})
    first = client.post("/runs", headers=AUTH, json={})
    assert first.status_code == 202
    try:
        # Two runs would share the node's CPU and disk and spoil each other's measurements.
        second = client.post("/runs", headers=AUTH, json={})
        assert second.status_code == 409
        assert client.get(f"/runs/{first.json()['run_id']}", headers=AUTH).json() == {"state": "RUNNING"}
    finally:
        release.set()
    assert wait_for(client, first.json()["run_id"])["state"] == "DONE"
    assert client.post("/runs", headers=AUTH, json={}).status_code == 202
    release.set()


def test_failed_run_is_reported_cleans_up_its_clone_and_frees_the_node(client, monkeypatch):
    def failing_run(*args):
        raise RuntimeError("clone did not finish recovery")

    monkeypatch.setattr(runner, "run", failing_run)
    run_id = client.post("/runs", headers=AUTH, json={}).json()["run_id"]
    assert wait_for(client, run_id) == {"state": "FAILED", "error": "RuntimeError: clone did not finish recovery"}
    deadline = time.monotonic() + 5
    while agent_app.busy.locked() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not agent_app.busy.locked()
    assert client.stopped == [pg.WORK]  # the half-used clone is not left running
    # The agent itself is unharmed: the next run is accepted.
    monkeypatch.setattr(runner, "run", lambda *a: {"arms": {}})
    assert client.post("/runs", headers=AUTH, json={}).status_code == 202


def test_unknown_run_is_not_found(client):
    assert client.get("/runs/nope", headers=AUTH).status_code == 404


def test_capture_pruning_removes_only_old_capture_files(tmp_path):
    now = time.time()
    old, recent, other = tmp_path / "pg-07.json", tmp_path / "pg-42.json", tmp_path / "notes.txt"
    for path, age_min in ((old, 30), (recent, 2), (other, 30)):
        path.write_text("x")
        os.utime(path, (now - age_min * 60, now - age_min * 60))
    assert agent_app.prune_capture(str(tmp_path), minutes=15) == 1
    assert not old.exists() and recent.exists() and other.exists()


# ── Orchestration of a run ───────────────────────────────────────────────────

class FakeSource:
    """Stands in for a connection to the source: only the tenant list is asked for."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        return [("t_a",), ("t_b",)]


def captured(role: str, statements: int = 1) -> Transaction:
    start = T0 + timedelta(seconds=1)
    return Transaction(user=role, app="oltp", pid=1, start=start, end=start,
                       statements=[Statement("SELECT 1", {}, 1.0, None)] * statements)


@pytest.fixture
def orchestration(monkeypatch):
    """runner.run with freezing, capture and arms replaced; records what each arm was given."""
    calls = []

    def fake_arm(action, transactions, t0):
        calls.append({"arm": "treatment" if action is not None else "control", "transactions": transactions})
        return {"samples": {"t_a/OLTP": [(0.0, 1.0)]}, "errors": {"t_a/OLTP": 1}, "wal_bytes": 100,
                "error_examples": {}, "prepare_s": 0.1}

    window = {}

    def fake_read_window(directory, start, end):
        window.update(start=start, end=end)
        return [captured("t_a"), captured("t_b"), captured("postgres"), captured("dbpilot_monitor"),
                captured("t_a", statements=0)]

    monkeypatch.setattr(pg, "freeze_base", lambda: (T0, "0/1000", 0.5))
    monkeypatch.setattr(pg, "connect", lambda port: FakeSource())
    monkeypatch.setattr(runner, "read_window", fake_read_window)
    monkeypatch.setattr(runner, "_arm", fake_arm)
    return calls, window


ANALYZE = {"type": "analyze", "table": "orders"}


def test_only_tenant_transactions_with_statements_are_replayed(orchestration):
    calls, _ = orchestration
    result = runner.run(ANALYZE, window_s=20)
    assert result["transactions"] == 2
    assert [t.user for t in calls[0]["transactions"]] == ["t_a", "t_b"]
    # Both arms are given the same transactions.
    assert calls[0]["transactions"] is calls[1]["transactions"]


def test_window_starts_at_t0_and_is_cut_at_the_present(orchestration):
    _, window = orchestration
    result = runner.run(ANALYZE, window_s=20)
    assert window["start"] == T0 and window["end"] == T0 + timedelta(seconds=20) and result["window_s"] == 20
    result = runner.run(ANALYZE, window_s=600)
    assert window["end"] <= datetime.now(timezone.utc) and result["window_s"] < 60


@pytest.mark.parametrize("repetitions, treatment_first, order", [
    (1, False, ["control", "treatment"]),
    (1, True, ["treatment", "control"]),
    (3, False, ["control", "treatment", "treatment", "control", "control", "treatment"]),
    (2, True, ["treatment", "control", "control", "treatment"]),
])
def test_arm_order_alternates_so_neither_arm_always_runs_first(orchestration, repetitions, treatment_first, order):
    calls, _ = orchestration
    runner.run(ANALYZE, window_s=20, repetitions=repetitions, treatment_first=treatment_first)
    assert [c["arm"] for c in calls] == order


def test_repetitions_are_pooled_per_arm(orchestration):
    result = runner.run(ANALYZE, window_s=20, repetitions=3)
    for arm in ("control", "treatment"):
        assert len(result["arms"][arm]["samples"]["t_a/OLTP"]) == 3
        assert result["arms"][arm]["errors"] == {"t_a/OLTP": 3}
        assert result["arms"][arm]["wal_bytes"] == 300


def test_action_is_validated_before_anything_is_frozen(monkeypatch):
    def must_not_run():
        raise AssertionError("the source was frozen for an action that cannot be evaluated")

    monkeypatch.setattr(pg, "freeze_base", must_not_run)
    for action in ({"type": "replica_routing", "tenant_role": "t_a", "enabled": True},
                   {"type": "query_rewrite", "queryid": "1", "suggestion": "use a join"},
                   {"type": "no_action", "reason": "nothing to do"}):
        with pytest.raises(ValueError, match="cannot be evaluated on the twin"):
            runner.run(action, window_s=20)
    with pytest.raises(ValueError):
        runner.run({"type": "instance_setting", "name": "fsync", "value": "off"}, window_s=20)
