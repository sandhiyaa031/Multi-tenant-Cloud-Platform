"""One twin run: control and treatment replays of the same captured window."""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from agent import pg
from agent.replay import replay
from dbpilot_core import actions
from dbpilot_core.pglog import read_window

LOG_DIR = os.environ.get("DP_LOG_DIR", "/var/log/dbpilot")
INSTANCE_CPUS = os.environ.get("TWIN_INSTANCE_CPUS")  # e.g. "12-15", matching the primary's core count


def _arm(action: actions.Action | None, transactions, t0: datetime) -> dict:
    prepare_s = pg.fresh_work(INSTANCE_CPUS)
    result: dict = {"prepare_s": round(prepare_s, 2)}
    with pg.connect(pg.WORK_PORT) as conn:
        size_before = conn.execute("SELECT pg_database_size('app')").fetchone()[0]
        if action is not None:
            started = time.monotonic()
            plan = actions.plan(action, conn)
            actions.run(plan.apply, conn)
            result.update(apply_s=round(time.monotonic() - started, 2), applied=plan.apply,
                          inverse=plan.inverse, cheap_to_undo=plan.cheap_to_undo, notes=plan.notes)
        size_after = conn.execute("SELECT pg_database_size('app')").fetchone()[0]
        result["storage_delta_bytes"] = size_after - size_before
        limits = dict(conn.execute(
            "SELECT r.rolname, r.rolconnlimit FROM pg_roles r JOIN ch.tenant_map m ON m.db_role = r.rolname"
        ).fetchall())
        # Both arms start the replay from the same point: nothing left to flush from
        # promotion or from applying the action.
        conn.execute("CHECKPOINT")
        wal_before = conn.execute("SELECT pg_current_wal_lsn()").fetchone()[0]

    samples, errors, examples = asyncio.run(replay(transactions, t0, pg.WORK_PORT, limits))

    with pg.connect(pg.WORK_PORT) as conn:
        result["wal_bytes"] = int(conn.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)", (wal_before,)).fetchone()[0])
    pg.stop(pg.WORK)
    result.update(samples=samples, errors=errors, error_examples=examples)
    return result


def run(action_data: dict | None, window_s: float, repetitions: int = 1) -> dict:
    """`action_data` None runs an A/A test: two identical arms, to measure the noise floor."""
    action = actions.parse_action(action_data) if action_data else None
    if action is not None and not isinstance(action, actions.EXECUTABLE):
        raise ValueError(f"{action.type} cannot be evaluated on the twin")

    t0, lsn, copy_s = pg.freeze_base()
    # Never ask for more workload than has been captured since T0.
    t1 = min(t0 + timedelta(seconds=window_s), datetime.now(timezone.utc))
    with pg.connect(pg.SOURCE_PORT) as conn:
        tenants = {r[0] for r in conn.execute("SELECT db_role FROM ch.tenant_map")}
    transactions = [t for t in read_window(LOG_DIR, t0, t1) if t.user in tenants and t.statements]

    arms: dict[str, dict] = {}
    # The order alternates (control-treatment, treatment-control, ...) so that slow
    # drift in the host, or an advantage of running first, falls on both arms alike.
    for rep in range(repetitions):
        order = [("control", None), ("treatment", action)]
        if rep % 2:
            order.reverse()
        for name, arm_action in order:
            outcome = _arm(arm_action, transactions, t0)
            if name not in arms:
                arms[name] = outcome
            else:
                for key, values in outcome["samples"].items():
                    arms[name]["samples"].setdefault(key, []).extend(values)
                for key, count in outcome["errors"].items():
                    arms[name]["errors"][key] = arms[name]["errors"].get(key, 0) + count
                arms[name]["wal_bytes"] += outcome["wal_bytes"]

    return {
        "t0": t0.isoformat(), "t1": t1.isoformat(), "lsn": lsn, "window_s": (t1 - t0).total_seconds(),
        "base_copy_s": round(copy_s, 2), "transactions": len(transactions), "repetitions": repetitions,
        "action": action.model_dump() if action else None,
        "arms": arms,
    }
