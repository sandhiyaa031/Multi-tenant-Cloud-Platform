"""The twin's instances: the delayed source, the frozen base, the working clone.

What these prove is the property the whole design rests on: a clone is a real
standalone database at one known past moment of production, changing it changes
nothing else, and every arm starts again from that same moment.
"""
import pytest

from agent import pg

from .conftest import (DELAY_S, PRIMARY_PORT, applied_marker, note_count, primary, scalar, source_caught_up,
                       source_received, wait_until, write_marker)

pytestmark = pytest.mark.usefixtures("cluster", "idle_work")


def index_exists(port: int, name: str) -> bool:
    return scalar(port, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'ch' AND indexname = %s", (name,)) == 1


def restart_source_with_nothing_replayed() -> None:
    """Puts the source in the state it is in after the twin node restarts while
    production is idle: running, but with no transaction replayed since it started."""
    with primary() as conn:
        conn.execute("CHECKPOINT")
    wait_until(source_caught_up, what="the source to catch up")
    with pg.connect(pg.SOURCE_PORT) as conn:
        conn.execute("CHECKPOINT")  # a restartpoint: the restart replays nothing older than this
    pg.stop(pg.SOURCE)
    pg.start(pg.SOURCE, pg.SOURCE_PORT)
    assert pg.source_status()["replay_timestamp"] is None


def test_source_is_a_standby_of_production_that_applies_changes_late():
    assert scalar(pg.SOURCE_PORT, "SELECT pg_is_in_recovery()") is True
    assert f"recovery_min_apply_delay = '{DELAY_S}s'" in (pg.SOURCE / "postgresql.auto.conf").read_text()
    status = pg.source_status()
    assert status["configured_delay_s"] == DELAY_S and status["pause_state"] == "not paused"

    lsn = write_marker("late")
    wait_until(lambda: source_received(lsn), what="the source to receive the marker")
    # Received at once, applied only after the delay: the source is in production's past.
    assert note_count(pg.SOURCE_PORT, "late") == 0
    wait_until(lambda: note_count(pg.SOURCE_PORT, "late") == 1, what="the source to apply the marker")


def test_ensure_source_leaves_a_running_source_alone():
    pid = (pg.SOURCE / "postmaster.pid").read_text().splitlines()[0]
    pg.ensure_source()
    assert (pg.SOURCE / "postmaster.pid").read_text().splitlines()[0] == pid


def test_source_is_read_only():
    with pg.connect(pg.SOURCE_PORT) as conn, pytest.raises(pg.psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id) VALUES (1, 0, 0)")


def test_freeze_that_cannot_proceed_does_not_leave_replay_paused():
    """Regression: with nothing replayed yet there is no T0, and the freeze refuses.
    It used to refuse after pausing replay and never resume it, so the source
    stopped following production and every later run refused for the same reason."""
    restart_source_with_nothing_replayed()
    with pytest.raises(RuntimeError, match="has not replayed any transaction"):
        pg.freeze_base()
    assert pg.source_status()["pause_state"] == "not paused"
    assert pg.is_running(pg.SOURCE)

    # Once production commits something, the same source can be frozen.
    applied_marker("after-refusal")
    t0, _, _ = pg.freeze_base()
    assert t0 is not None


def test_clone_is_a_standalone_database_at_exactly_the_frozen_moment():
    applied_marker("before-t0")
    lsn = write_marker("after-t0")
    wait_until(lambda: source_received(lsn), what="the source to receive the later marker")
    assert note_count(pg.SOURCE_PORT, "after-t0") == 0

    t0, frozen_lsn, _ = pg.freeze_base()
    # The copy holds the later marker's WAL; only a recovery target that stops short of it
    # keeps it from being applied. (Regression: an inclusive target applied that one commit.)
    conf = (pg.BASE / "postgresql.auto.conf").read_text()
    assert f"recovery_target_lsn = '{frozen_lsn}'" in conf and "recovery_target_inclusive = false" in conf
    assert "recovery_min_apply_delay = 0" in conf
    assert "primary_conninfo = ''" in conf  # a clone must never connect back to production

    pg.fresh_work()
    assert scalar(pg.WORK_PORT, "SELECT pg_is_in_recovery()") is False
    assert note_count(pg.WORK_PORT, "before-t0") == 1
    assert note_count(pg.WORK_PORT, "after-t0") == 0
    assert t0 is not None

    # Freezing stopped the source only for the copy: it is following production again.
    assert pg.source_status()["pause_state"] == "not paused"
    wait_until(lambda: note_count(pg.SOURCE_PORT, "after-t0") == 1, what="the source to resume replay")


def test_clone_of_an_idle_production_is_promoted_directly():
    """With no WAL beyond T0 a recovery target would never be reached and the clone would wait forever."""
    applied_marker("idle")
    wait_until(source_caught_up, what="the source to catch up")
    pg.freeze_base()
    assert (pg.BASE / "promote_manually").exists()
    assert "recovery_target_lsn" not in (pg.BASE / "postgresql.auto.conf").read_text()

    pg.fresh_work()
    assert scalar(pg.WORK_PORT, "SELECT pg_is_in_recovery()") is False
    assert note_count(pg.WORK_PORT, "idle") == 1


def test_changes_on_the_clone_reach_nothing_else_and_do_not_survive_the_next_arm():
    applied_marker("isolation")
    pg.freeze_base()
    pg.fresh_work()
    with pg.connect(pg.WORK_PORT) as conn:
        conn.execute("CREATE INDEX only_on_clone ON ch.order_line_t_a (ol_i_id)")
        conn.execute("INSERT INTO ch.order_line (ol_w_id, ol_o_id, ol_i_id, note) VALUES (1, 0, 0, 'clone-only')")
        conn.execute("ALTER ROLE t_a CONNECTION LIMIT 3")
    assert index_exists(pg.WORK_PORT, "only_on_clone") and note_count(pg.WORK_PORT, "clone-only") == 1

    # Production and the source never see it, however long replay runs.
    write_marker("isolation-later")
    wait_until(lambda: note_count(pg.SOURCE_PORT, "isolation-later") == 1, what="the source to keep replaying")
    for port in (PRIMARY_PORT, pg.SOURCE_PORT):
        assert not index_exists(port, "only_on_clone")
        assert note_count(port, "clone-only") == 0
        assert scalar(port, "SELECT rolconnlimit FROM pg_roles WHERE rolname = 't_a'") == -1
    # Nor does the clone see what production did after T0.
    assert note_count(pg.WORK_PORT, "isolation-later") == 0

    # The next arm starts from the frozen base again, not from the last arm's leftovers.
    pg.fresh_work()
    assert not index_exists(pg.WORK_PORT, "only_on_clone")
    assert note_count(pg.WORK_PORT, "clone-only") == 0
    assert scalar(pg.WORK_PORT, "SELECT rolconnlimit FROM pg_roles WHERE rolname = 't_a'") == -1
    assert note_count(pg.WORK_PORT, "isolation") == 1


def test_stop_is_safe_to_repeat_and_copy_replaces_what_was_there():
    applied_marker("cleanup")
    pg.freeze_base()
    pg.fresh_work()
    pg.stop(pg.WORK)
    assert not pg.is_running(pg.WORK)
    pg.stop(pg.WORK)  # already stopped: nothing to do, no error

    (pg.WORK / "leftover").write_text("from an earlier arm")
    pg.copy(pg.BASE, pg.WORK)
    assert not (pg.WORK / "leftover").exists()
    assert not (pg.WORK / "postmaster.pid").exists()


def test_source_survives_a_failed_copy(monkeypatch):
    """The source is stopped for the copy. Whatever the copy does, it must be started again."""
    applied_marker("copy-fails")

    def failing_copy(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(pg, "copy", failing_copy)
    with pytest.raises(OSError):
        pg.freeze_base()
    assert pg.is_running(pg.SOURCE)
    assert pg.source_status()["pause_state"] == "not paused"
    write_marker("copy-fails-later")
    wait_until(lambda: note_count(pg.SOURCE_PORT, "copy-fails-later") == 1, what="the source to keep replaying")


def test_start_reports_why_an_instance_would_not_start(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    with pytest.raises(RuntimeError, match="could not start broken"):
        pg.start(broken, 5999)
