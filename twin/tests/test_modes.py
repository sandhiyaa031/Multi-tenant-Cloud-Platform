"""The two procedures that keep both arms up at once (simultaneous, interleaved), and the
per-template timing every replay now reports. Transactions are built by hand, so what must
come out is known exactly."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from agent import pg, runner
from agent.replay import summarise_templates
from dbpilot_core.pglog import Statement, Transaction

from .conftest import PRIMARY_PORT, applied_marker, note_count, scalar

pytestmark = pytest.mark.usefixtures("cluster", "idle_work")

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
INDEX_FOR_T_A = {"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_a"}
LOOKUP, WRITE = 111, 222      # query ids, as production's log would carry them


def txn(role: str, statements: list[tuple[str, int]], offset_s: float, app: str = "oltp") -> Transaction:
    start = T0 + timedelta(seconds=offset_s)
    return Transaction(user=role, app=app, pid=1, start=start, end=start,
                       statements=[Statement(sql, {}, 1.0, qid) for sql, qid in statements])


def insert(note: str, w_id: int = 1) -> tuple[str, int]:
    return f"INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id, note) VALUES ({w_id}, 0, 0, '{note}')", WRITE


def workload(tag: str, seconds: float = 2.5) -> list[Transaction]:
    """Five transactions per tenant spread over `seconds`: a write and a lookup each."""
    out = []
    for i in range(5):
        at = seconds * i / 5
        out.append(txn("t_a", [insert(f"{tag}-a{i}"), ("SELECT count(*) FROM ch.order_line WHERE ol_i_id = 0", LOOKUP)], at))
        out.append(txn("t_b", [insert(f"{tag}-b{i}", w_id=3)], at + 0.05))
    return out


@pytest.fixture
def frozen():
    applied_marker(f"modes-base-{uuid.uuid4().hex[:8]}")
    pg.freeze_base()
    yield
    pg.stop(pg.WORK)
    pg.stop(pg.WORK_B)


def test_template_summary_gives_percentiles_per_query_id():
    got = summarise_templates({"t_a/OLTP": {"7": [float(v) for v in range(1, 101)]}}, {"7": "SELECT 1"})
    assert got == {"t_a/OLTP": {"7": {"n": 100, "mean_ms": 50.5, "p50_ms": 50.0, "p95_ms": 95.0, "p99_ms": 99.0,
                                      "sql": "SELECT 1"}}}


@pytest.mark.parametrize("treatment_first", [False, True])
def test_simultaneous_arms_each_replay_the_whole_window_on_their_own_clone(frozen, treatment_first):
    tag = uuid.uuid4().hex[:6]
    transactions = workload(tag)
    arms, how = runner._simultaneous(None, transactions, T0, treatment_first)

    assert set(arms) == {"control", "treatment"}
    # Which clone the treatment gets follows the flag, so a caller can alternate it.
    assert (arms["treatment"]["slot"], arms["control"]["slot"]) == (("a", "b") if treatment_first else ("b", "a"))
    for arm in arms.values():
        assert {k: len(v) for k, v in arm["samples"].items()} == {"t_a/OLTP": 5, "t_b/OLTP": 5}
        assert arm["errors"] == {} and arm["wal_bytes"] > 0
        # Per query template: every statement that carried a query id was timed.
        assert arm["templates"]["t_a/OLTP"][str(LOOKUP)]["n"] == 5 and arm["templates"]["t_a/OLTP"][str(WRITE)]["n"] == 5
        assert arm["templates"]["t_b/OLTP"][str(WRITE)]["p95_ms"] > 0
    assert how["started_together_at"] > 0
    # Both clones are stopped afterwards, and production saw none of it.
    assert not pg.is_running(pg.WORK) and not pg.is_running(pg.WORK_B)
    assert note_count(PRIMARY_PORT, f"{tag}-a0") == 0


def test_interleaved_arms_replay_every_block_once_each_in_a_seeded_order(frozen):
    tag = uuid.uuid4().hex[:6]
    transactions = workload(tag, seconds=2.5)
    t1 = T0 + timedelta(seconds=2.5)
    arms, how = runner._interleaved(None, transactions, T0, t1, False, seed=7)
    again = runner._interleaved(None, workload(uuid.uuid4().hex[:6]), T0, t1, False, seed=7)[1]

    assert how["blocks"] == runner.BLOCKS and how["block_s"] == 0.5 and len(how["first_arm_per_block"]) == runner.BLOCKS
    assert set(how["first_arm_per_block"]) <= {"control", "treatment"}
    assert again["first_arm_per_block"] == how["first_arm_per_block"]            # the seed fixes the order
    for arm in arms.values():
        assert {k: len(v) for k, v in arm["samples"].items()} == {"t_a/OLTP": 5, "t_b/OLTP": 5}
        assert arm["errors"] == {}
        # Offsets are those of the original window, whichever block a transaction fell in.
        assert sorted(offset for offset, _ in arm["samples"]["t_a/OLTP"]) == [0.0, 0.5, 1.0, 1.5, 2.0]


def test_the_action_reaches_the_treatment_clone_only_when_both_are_up(frozen):
    captured = {}
    real_finish = runner._finish

    def spy(result, slot, wal_before, measured):
        port = runner.SLOTS[slot][1]
        captured[slot] = scalar(port, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'ch' AND indexname LIKE 'dbp_%%'")
        return real_finish(result, slot, wal_before, measured)

    runner._finish = spy
    try:
        arms, _ = runner._simultaneous(runner.actions.parse_action(INDEX_FOR_T_A), workload(uuid.uuid4().hex[:6]), T0, True)
    finally:
        runner._finish = real_finish
    assert captured == {"a": 1, "b": 0}                      # treatment_first put the treatment on slot a
    assert arms["treatment"]["applied"] and "applied" not in arms["control"]


def test_an_unknown_mode_and_several_repetitions_of_a_paired_mode_are_refused():
    with pytest.raises(ValueError, match="unknown mode"):
        runner.run(None, window_s=20, mode="alternating")
    with pytest.raises(ValueError, match="one pair per call"):
        runner.run(None, window_s=20, repetitions=2, mode="simultaneous")
