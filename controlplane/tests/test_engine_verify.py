"""The engine's verification path, with the twin node replaced by a stand-in.

The stand-in returns replay measurements whose true effect is known, in the
shape the twin agent returns them. What is under test is what the engine does
with them: how many replays it asks for, what it records, and which state the
proposal ends in. Nothing here reaches a data plane.
"""
import os

import httpx
import numpy as np
import pytest

from app.engine import Config, Engine

TARGET, NEIGHBOUR = "t_analytic/OLAP", "t_steady/OLTP"
INDEX = {"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_analytic"}
WINDOW_S = 120.0
MAX_LOOKS = 4


def stream(n: int, mean_ms: float, seed: int, noise: float = 0.01) -> list[list[float]]:
    """`n` transactions spread evenly over the window, log-normal latencies."""
    rng = np.random.default_rng(seed)
    times = np.linspace(0, WINDOW_S, n, endpoint=False)
    return [[float(t), float(v)] for t, v in zip(times, rng.lognormal(np.log(mean_ms), noise, n))]


def twin_result(target_factor: float, neighbour_factor: float, neighbour_n: int = 600, seed: int = 0,
                neighbour_noise: float = 0.01) -> dict:
    """One twin run (one replay pair): treatment latency = control latency x factor, per tenant."""
    def arm(target: float, neighbour: float, offset: int, **extra) -> dict:
        return {"samples": {TARGET: stream(600, 100 * target, seed + offset),
                            NEIGHBOUR: stream(neighbour_n, 10 * neighbour, seed + offset + 1, neighbour_noise)},
                "errors": {}, "wal_bytes": 1000, "storage_delta_bytes": 0, **extra}

    return {"window_s": WINDOW_S, "transactions": 600 + neighbour_n, "repetitions": 1,
            "arms": {"control": arm(1.0, 1.0, 0),
                     "treatment": arm(target_factor, neighbour_factor, 100, apply_s=0.4, cheap_to_undo=True,
                                      storage_delta_bytes=4096)}}


class FakeTwin:
    """Stands in for Engine.twin_run. `script` holds one entry per replay: a result or an exception."""

    def __init__(self, script: list):
        self.script, self.calls = list(script), []

    def __call__(self, action: dict, treatment_first: bool = False) -> dict:
        self.calls.append({"action": action, "treatment_first": treatment_first})
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeWhatIf:
    """Stands in for the HTTP client the engine uses for the planner what-if (T1)."""

    def __init__(self, result: dict | Exception):
        self.result = result

    def post(self, path: str, json: dict):
        if isinstance(self.result, Exception):
            raise self.result
        return httpx.Response(200, json=self.result, request=httpx.Request("POST", "http://twin" + path))


@pytest.fixture
def engine():
    return Engine(Config(control_url=os.environ["CONTROL_DB_ENGINE_URL"], twin_url="http://twin.invalid",
                         twin_token="unused", executor_user="unused", executor_password="unused",
                         pooler_admin_user="unused", pooler_admin_password="unused",
                         twin_window_s=WINDOW_S, twin_repetitions=1, twin_max_looks=MAX_LOOKS))


@pytest.fixture
def cluster(client, org):
    cluster_id = org.cluster()
    for name, role, lo, hi in (("analytic", "t_analytic", 5, 8), ("steady", "t_steady", 1, 2)):
        body = {"cluster_id": cluster_id, "name": name, "db_role": role, "warehouse_lo": lo, "warehouse_hi": hi,
                "profile": "STEADY_OLTP"}
        assert client.post("/api/v1/tenants", headers=org.admin, json=body).status_code == 201
    return cluster_id


def verify(engine, client, org, cluster_id, script, *, action=INDEX, whatif=None, **options):
    """Proposes through the API, runs the engine's verification on it, returns (detail, twin)."""
    r = client.post(f"/api/v1/clusters/{cluster_id}/proposals", headers=org.admin, json={"action": action, **options})
    assert r.status_code == 201, r.text
    twin = FakeTwin(script)
    engine.twin_run = twin
    engine.http = FakeWhatIf(whatif or {"supported": True, "explained": 2, "improved": 1, "queries": []})
    with engine.control() as db:
        proposal = db.execute("UPDATE cp.proposals SET state = 'VERIFYING' WHERE id = %s RETURNING *",
                              (r.json()["id"],)).fetchone()
        engine.verify(db, proposal)
    detail = client.get(f"/api/v1/proposals/{r.json()['id']}", headers=org.admin).json()
    return detail, twin


def steps(detail: dict) -> list[tuple[str, str]]:
    return [(s["tier"], s["decision"]) for s in detail["steps"]]


def pairs(target_factor: float, neighbour_factor: float, **options) -> list[dict]:
    """A script of MAX_LOOKS replay pairs with the same true effect and independent noise."""
    return [twin_result(target_factor, neighbour_factor, seed=s * 1000, **options) for s in range(MAX_LOOKS)]


def test_clear_benefit_with_an_unharmed_neighbour_is_approved_once_pairs_agree(engine, client, org, cluster):
    detail, twin = verify(engine, client, org, cluster, pairs(0.5, 1.0))
    # One replay pair decides nothing; the verdict comes as soon as enough pairs agree.
    assert 2 <= len(twin.calls) <= MAX_LOOKS and twin.calls[0]["action"]["type"] == "create_index"
    assert steps(detail) == [("T0", "APPROVE"), ("T1", "APPROVE"), ("T2", "APPROVE")]
    # Passing verification is not approval: without auto-approve a person still decides.
    assert detail["state"] == "AWAITING_APPROVAL"
    run = detail["twin_runs"][0]
    assert run["repetitions"] == len(twin.calls) and run["replay_errors"] == 0 and run["storage_delta_bytes"] == 4096
    verdict = run["verdict"]
    assert verdict["looks"] == len(twin.calls)
    assert verdict["effects"][TARGET]["status"] == "BENEFITS" and verdict["effects"][NEIGHBOUR]["status"] == "SAFE"
    # The other gate mode's verdict on the same measurements is recorded, and decides nothing.
    assert verdict["shadow"]["mode"] == "aggregate"
    assert verdict["calibration"] == {"contract_tolerance": 0.1, "history_pairs": 0}


def test_auto_approve_applies_only_to_a_firm_approval(engine, client, org, cluster):
    detail, _ = verify(engine, client, org, cluster, pairs(0.5, 1.0), auto_approve=True)
    assert detail["state"] == "APPROVED"


def test_harm_to_a_neighbour_rejects_however_much_the_target_gains(engine, client, org, cluster):
    detail, twin = verify(engine, client, org, cluster, pairs(0.3, 1.4), auto_approve=True)
    # Harm is final once it has been seen with each arm running first: no further replay is spent on it.
    assert 2 <= len(twin.calls) < MAX_LOOKS
    assert {c["treatment_first"] for c in twin.calls} == {False, True}
    assert detail["state"] == "REJECTED"
    assert NEIGHBOUR in detail["state_reason"]
    assert detail["twin_runs"][0]["verdict"]["effects"][NEIGHBOUR]["status"] == "HARMED"


def test_one_disturbed_replay_is_not_harm(engine, client, org, cluster):
    """The neighbour is three times slower in the treatment arm of the first replay only: something
    else on the machine, not the action. The later pairs disagree with it, so nothing is shown."""
    script = [twin_result(0.5, 3.0, seed=7)] + pairs(0.5, 1.0)[1:]
    detail, twin = verify(engine, client, org, cluster, script, auto_approve=True)
    assert len(twin.calls) == MAX_LOOKS
    assert detail["state"] == "INCONCLUSIVE"
    assert detail["twin_runs"][0]["verdict"]["effects"][NEIGHBOUR]["status"] == "UNCERTAIN"


def test_uncertain_verdict_buys_more_replays_and_is_never_applied(engine, client, org, cluster):
    # Too few neighbour transactions in any number of replays to show it is unharmed.
    script = [twin_result(0.5, 1.0, neighbour_n=5, seed=s) for s in range(MAX_LOOKS)]
    detail, twin = verify(engine, client, org, cluster, script, auto_approve=True)
    assert len(twin.calls) == MAX_LOOKS
    # The arm that runs first alternates between replays.
    assert [c["treatment_first"] for c in twin.calls] == [False, True, False, True]
    assert detail["state"] == "INCONCLUSIVE"  # auto-approve does not cover an uncertain result
    assert steps(detail)[-1] == ("T2", "INCONCLUSIVE")
    run = detail["twin_runs"][0]
    assert run["verdict"]["looks"] == MAX_LOOKS and run["repetitions"] == MAX_LOOKS
    assert f"[{MAX_LOOKS} replay(s)]" in detail["steps"][-1]["summary"]


def test_a_later_replay_can_settle_what_the_first_could_not(engine, client, org, cluster):
    # 30 neighbour transactions per replay, fewer once the warm-up is dropped: one replay is
    # below the minimum the gate will judge. Its pairs still count once there are enough of them.
    script = pairs(0.5, 1.0, neighbour_n=30, neighbour_noise=0.002)
    detail, twin = verify(engine, client, org, cluster, script)
    assert len(twin.calls) > 1
    assert detail["state"] == "AWAITING_APPROVAL"
    run = detail["twin_runs"][0]
    assert run["verdict"]["looks"] == len(twin.calls) and run["transactions"] == len(twin.calls) * 630
    assert run["verdict"]["effects"][NEIGHBOUR]["status"] == "SAFE"


def test_failed_twin_run_is_inconclusive_never_approved(engine, client, org, cluster):
    detail, twin = verify(engine, client, org, cluster, [RuntimeError("clone did not finish recovery")],
                          auto_approve=True)
    assert len(twin.calls) == 1
    assert detail["state"] == "INCONCLUSIVE"
    assert "twin run failed" in detail["state_reason"]
    assert steps(detail)[-1] == ("T2", "INCONCLUSIVE") and detail["twin_runs"] == []


def test_failure_of_a_later_replay_keeps_the_earlier_uncertain_verdict(engine, client, org, cluster):
    script = [twin_result(0.5, 1.0, neighbour_n=5), TimeoutError("twin run did not finish")]
    detail, twin = verify(engine, client, org, cluster, script, auto_approve=True)
    assert len(twin.calls) == 2
    assert detail["state"] == "INCONCLUSIVE"
    assert "replay 2 failed" in detail["steps"][-1]["summary"]
    assert detail["twin_runs"][0]["verdict"]["looks"] == 1


def test_index_the_planner_would_not_use_is_rejected_before_any_replay(engine, client, org, cluster):
    detail, twin = verify(engine, client, org, cluster, [],
                          whatif={"supported": True, "explained": 4, "improved": 0, "queries": []})
    assert twin.calls == []
    assert steps(detail) == [("T0", "APPROVE"), ("T1", "REJECT")]
    assert detail["state"] == "REJECTED"


def test_unavailable_whatif_does_not_decide_and_the_replay_still_runs(engine, client, org, cluster):
    detail, twin = verify(engine, client, org, cluster, pairs(0.5, 1.0),
                          whatif=httpx.ConnectError("twin unreachable"))
    assert steps(detail) == [("T0", "APPROVE"), ("T1", "SKIPPED"), ("T2", "APPROVE")]
    assert twin.calls and detail["state"] == "AWAITING_APPROVAL"


@pytest.mark.parametrize("mode", ["canary_only", "none"])
def test_comparison_modes_skip_the_twin_and_say_so(engine, client, org, cluster, mode):
    detail, twin = verify(engine, client, org, cluster, [], verification=mode)
    assert twin.calls == []
    assert steps(detail) == [("T0", "APPROVE"), ("T1", "SKIPPED"), ("T2", "SKIPPED")]
    assert detail["state"] == "APPROVED" and "twin skipped" in detail["state_reason"]


def test_advisory_action_goes_to_a_human_without_touching_the_twin(engine, client, org, cluster):
    action = {"type": "replica_routing", "tenant_role": "t_analytic", "enabled": True}
    detail, twin = verify(engine, client, org, cluster, [], action=action, auto_approve=True)
    assert twin.calls == [] and detail["state"] == "ADVISORY"


def test_aggregate_gate_approves_the_change_the_per_tenant_gate_rejects(engine, client, org, cluster):
    """The same measurements, judged the way single-tenant tuners judge them. The
    analytical target dominates total latency, so the neighbour's harm disappears."""
    detail, _ = verify(engine, client, org, cluster, pairs(0.3, 1.4), gate_mode="aggregate")
    assert detail["state"] == "AWAITING_APPROVAL"
    shadow = detail["twin_runs"][0]["verdict"]["shadow"]
    assert shadow["mode"] == "per_tenant" and shadow["decision"] == "REJECT"
