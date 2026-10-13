"""One twin run: control and treatment replays of the same captured window.

The two arms can be run in three ways (docs/evidence/bench_noise/protocol.md, Part 2):

    sequential    one arm after the other on the same clone slot; the procedure first built
    simultaneous  both clones up, each on its own CPU set, replayed at the same moment by two
                  replayer processes, so that whatever the machine does in that minute it does
                  to both arms
    interleaved   both clones up; the window is cut into blocks and for each block one arm is
                  replayed, then the other, in a random order, so that drift slower than a
                  block falls on both arms alike
"""
import asyncio
import multiprocessing
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone

from agent import pg
from agent.replay import Replayer, replay_full, replay_in_process
from dbpilot_core import actions
from dbpilot_core.pglog import read_window

LOG_DIR = os.environ.get("DP_LOG_DIR", "/var/log/dbpilot")
INSTANCE_CPUS = os.environ.get("TWIN_INSTANCE_CPUS")      # e.g. "12-15", matching the primary's core count
INSTANCE_CPUS_B = os.environ.get("TWIN_INSTANCE_CPUS_B")  # the second clone's cores, when two are up at once
REPLAY_CPUS = [int(c) for c in os.environ.get("TWIN_REPLAY_CPUS", "").split(",") if c.strip()]  # one per replayer process
MODES = ("sequential", "simultaneous", "interleaved")
BLOCKS = 5
SLOTS = {"a": (pg.WORK, pg.WORK_PORT, INSTANCE_CPUS), "b": (pg.WORK_B, pg.WORK_B_PORT, INSTANCE_CPUS_B)}


def _prepare(action: actions.Action | None, slot: str) -> tuple[dict, dict, str]:
    """A fresh clone in `slot`, with the action applied if there is one, warm and settled.
    Returns (what is known about the arm so far, connection limits per role, WAL position)."""
    workdir, port, cpus = SLOTS[slot]
    prepare_s = pg.fresh_work(cpus, workdir, port)
    result: dict = {"prepare_s": round(prepare_s, 2), "slot": slot}
    with pg.connect(port) as conn:
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
        # A fresh clone has an empty buffer cache, while production's is warm. Left alone,
        # whichever arm runs first pays for reading everything from disk and looks slower
        # for reasons unrelated to the action. Loading every relation into memory puts both
        # arms in the same warm state before measuring.
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
        conn.execute(
            "SELECT pg_prewarm(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'ch' AND c.relkind IN ('r', 'i')")
        # Both arms start the replay from the same point: nothing left to flush from
        # promotion or from applying the action.
        conn.execute("CHECKPOINT")
        wal_before = conn.execute("SELECT pg_current_wal_lsn()").fetchone()[0]
    return result, limits, wal_before


def _finish(result: dict, slot: str, wal_before: str, measured: dict) -> dict:
    workdir, port, _ = SLOTS[slot]
    with pg.connect(port) as conn:
        result["wal_bytes"] = int(conn.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)", (wal_before,)).fetchone()[0])
    pg.stop(workdir)
    result.update(measured)
    return result


def _arm(action: actions.Action | None, transactions, t0: datetime) -> dict:
    result, limits, wal_before = _prepare(action, "a")
    return _finish(result, "a", wal_before, asyncio.run(replay_full(transactions, t0, pg.WORK_PORT, limits)))


def _merge(into: dict, outcome: dict) -> None:
    for key, values in outcome["samples"].items():
        into["samples"].setdefault(key, []).extend(values)
    for key, count in outcome["errors"].items():
        into["errors"][key] = into["errors"].get(key, 0) + count
    into["wal_bytes"] += outcome["wal_bytes"]


def _sequential(action, transactions, t0, repetitions: int, treatment_first: bool) -> tuple[dict, dict]:
    arms: dict[str, dict] = {}
    # The order alternates (control-treatment, treatment-control, ...) so that slow
    # drift in the host, or an advantage of running first, falls on both arms alike.
    for rep in range(repetitions):
        order = [("control", None), ("treatment", action)]
        if (rep + treatment_first) % 2:
            order.reverse()
        for name, arm_action in order:
            outcome = _arm(arm_action, transactions, t0)
            if name not in arms:
                arms[name] = outcome
            else:
                _merge(arms[name], outcome)
    return arms, {}


def _both_up(action, treatment_first: bool) -> dict:
    """Both clones prepared. `treatment_first` puts the treatment arm on slot a, so a caller that
    alternates it also alternates which CPU set and which data directory the treatment gets."""
    slots = {"treatment": "a", "control": "b"} if treatment_first else {"control": "a", "treatment": "b"}
    prepared = {}
    for name in ("control", "treatment"):
        prepared[name] = (slots[name], *_prepare(action if name == "treatment" else None, slots[name]))
    return prepared


def _simultaneous(action, transactions, t0, treatment_first: bool) -> tuple[dict, dict]:
    prepared = _both_up(action, treatment_first)
    start_at = time.time() + 5.0
    arms, measured = {}, {}
    try:
        with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = {}
            for index, name in enumerate(("control", "treatment")):
                slot, result, limits, wal_before = prepared[name]
                cpu = REPLAY_CPUS[index if slot == "a" else 1 - index] if len(REPLAY_CPUS) >= 2 else None
                futures[name] = pool.submit(replay_in_process, transactions, t0, SLOTS[slot][1], limits, start_at, cpu)
            measured = {name: future.result(timeout=900) for name, future in futures.items()}
    finally:
        for name in ("control", "treatment"):
            slot, result, limits, wal_before = prepared[name]
            if name in measured:
                arms[name] = _finish(result, slot, wal_before, measured[name])
            else:
                pg.stop(SLOTS[slot][0])
    return arms, {"started_together_at": start_at}


def _interleaved(action, transactions, t0, t1, treatment_first: bool, seed: int) -> tuple[dict, dict]:
    prepared = _both_up(action, treatment_first)
    rng = random.Random(seed)
    block_s = (t1 - t0).total_seconds() / BLOCKS
    orders = []

    async def go() -> dict:
        replayers = {name: Replayer(SLOTS[prepared[name][0]][1], prepared[name][2]) for name in prepared}
        try:
            for replayer in replayers.values():
                await replayer.open(transactions)
            parts: list[list] = [[] for _ in range(BLOCKS)]
            for txn in transactions:
                parts[min(BLOCKS - 1, max(0, int((txn.start - t0).total_seconds() / block_s)))].append(txn)
            for block, part in enumerate(parts):
                begin = t0 + timedelta(seconds=block * block_s)
                order = ["control", "treatment"]
                rng.shuffle(order)
                orders.append(order[0])
                for name in order:
                    await replayers[name].run(part, t0, origin=begin)
        finally:
            for replayer in replayers.values():
                await replayer.close()
        return {name: replayer.result() for name, replayer in replayers.items()}

    arms = {}
    measured = None
    try:
        measured = asyncio.run(go())
    finally:
        for name in ("control", "treatment"):
            slot, result, limits, wal_before = prepared[name]
            if measured:
                arms[name] = _finish(result, slot, wal_before, measured[name])
            else:
                pg.stop(SLOTS[slot][0])
    return arms, {"blocks": BLOCKS, "block_s": round(block_s, 2), "first_arm_per_block": orders, "seed": seed}


def run(action_data: dict | None, window_s: float, repetitions: int = 1, treatment_first: bool = False,
        mode: str = "sequential", seed: int = 0) -> dict:
    """`action_data` None runs an A/A test: two identical arms, to measure the noise floor.
    `treatment_first` flips which arm starts (sequential) or which clone slot the treatment gets
    (the other two modes), so a caller that asks for one repetition at a time can still alternate
    between its calls."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; one of {MODES}")
    if mode != "sequential" and repetitions != 1:
        raise ValueError(f"{mode} runs one pair per call")
    action = actions.parse_action(action_data) if action_data else None
    if action is not None and not isinstance(action, actions.EXECUTABLE):
        raise ValueError(f"{action.type} cannot be evaluated on the twin")

    t0, lsn, copy_s = pg.freeze_base()
    # Never ask for more workload than has been captured since T0.
    t1 = min(t0 + timedelta(seconds=window_s), datetime.now(timezone.utc))
    with pg.connect(pg.SOURCE_PORT) as conn:
        tenants = {r[0] for r in conn.execute("SELECT db_role FROM ch.tenant_map")}
    transactions = [t for t in read_window(LOG_DIR, t0, t1) if t.user in tenants and t.statements]

    if mode == "sequential":
        arms, how = _sequential(action, transactions, t0, repetitions, treatment_first)
    elif mode == "simultaneous":
        arms, how = _simultaneous(action, transactions, t0, treatment_first)
    else:
        arms, how = _interleaved(action, transactions, t0, t1, treatment_first, seed)

    return {
        "t0": t0.isoformat(), "t1": t1.isoformat(), "lsn": lsn, "window_s": (t1 - t0).total_seconds(),
        "base_copy_s": round(copy_s, 2), "transactions": len(transactions), "repetitions": repetitions,
        "action": action.model_dump() if action else None, "mode": mode, "treatment_first": treatment_first, **how,
        "arms": arms,
    }
