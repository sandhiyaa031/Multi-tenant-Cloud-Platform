"""Manages the PostgreSQL instances of the experimentation plane.

    source   a standby of production whose replay is deliberately delayed, so it
             is always at a known past moment for which the workload is on file
    base     a frozen copy of the source at one such moment
    work     a copy of base, promoted to a standalone database and replayed
             against; recreated for every arm of every run
"""
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psycopg

ROOT = Path(os.environ.get("TWIN_ROOT", "/twin"))
SOURCE, BASE, WORK = ROOT / "source", ROOT / "base", ROOT / "work"
SOURCE_PORT, WORK_PORT = 5500, 5501
DELAY_S = int(os.environ.get("TWIN_DELAY_S", "180"))

# Same engine settings as the production primary, so plans and memory behaviour
# match. Statement logging is off: the twin is measured by the replayer.
CLONE_BUFFERS = os.environ.get("TWIN_SHARED_BUFFERS", "2GB")   # must equal the primary's
SOURCE_BUFFERS = os.environ.get("TWIN_SOURCE_BUFFERS", "256MB")  # the delayed standby only replays WAL
# The standby keeps recycled WAL segments up to max_wal_size, and every clone is a
# copy of its data directory: a small limit keeps that dead weight out of each copy.
SOURCE_OPTS = "-c max_wal_size=128MB -c min_wal_size=32MB"
ENGINE_OPTS = (
    "-c shared_preload_libraries=pg_stat_statements,auto_explain -c max_connections=200"
    " -c track_io_timing=on -c logging_collector=off -c log_min_duration_statement=-1"
    " -c listen_addresses=localhost -c hot_standby=on"
)


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def connect(port: int, dbname: str = "app", user: str = "postgres") -> psycopg.Connection:
    return psycopg.connect(host="127.0.0.1", port=port, dbname=dbname, user=user, autocommit=True)


def is_running(datadir: Path) -> bool:
    return _run("pg_ctl", "-D", str(datadir), "status", check=False).returncode == 0


def start(datadir: Path, port: int, cpus: str | None = None) -> None:
    sizing = f"-c shared_buffers={SOURCE_BUFFERS} {SOURCE_OPTS}" if datadir == SOURCE else f"-c shared_buffers={CLONE_BUFFERS}"
    cmd = ["pg_ctl", "-D", str(datadir), "-w", "-t", "300", "-l", str(datadir.parent / f"{datadir.name}.log"),
           "-o", f"-p {port} {sizing} {ENGINE_OPTS}", "start"]
    if cpus:
        # Pin the instance to its own cores so the replayer does not compete with it.
        cmd = ["taskset", "-c", cpus, *cmd]
    result = _run(*cmd, check=False)
    if result.returncode != 0:
        log = (datadir.parent / f"{datadir.name}.log")
        tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
        raise RuntimeError(f"could not start {datadir.name}: {result.stderr}\n{tail}")


def stop(datadir: Path) -> None:
    if is_running(datadir):
        _run("pg_ctl", "-D", str(datadir), "-w", "-t", "300", "-m", "fast", "stop")


def copy(src: Path, dst: Path) -> float:
    """Copies a stopped data directory. --reflink=auto shares blocks on filesystems
    that support it (XFS, btrfs), making the copy near-instant; elsewhere it is a full copy."""
    started = time.monotonic()
    if dst.exists():
        shutil.rmtree(dst)
    _run("cp", "-a", "--reflink=auto", str(src), str(dst))
    (dst / "postmaster.pid").unlink(missing_ok=True)
    return time.monotonic() - started


def bootstrap_source() -> None:
    """First start: base-backup the production primary and follow it with a delay."""
    if (SOURCE / "PG_VERSION").exists():
        return
    SOURCE.mkdir(parents=True, exist_ok=True)
    os.chmod(SOURCE, 0o700)
    env = {**os.environ, "PGPASSWORD": os.environ["REPLICATION_PASSWORD"]}
    subprocess.run(
        ["pg_basebackup", "-h", os.environ["PRIMARY_HOST"], "-p", "5432", "-U", "replicator",
         "-D", str(SOURCE), "-R", "-X", "stream",
         # Without this the backup waits for the primary's next spread checkpoint: minutes.
         "--checkpoint=fast"],
        check=True, env=env,
    )
    with open(SOURCE / "postgresql.auto.conf", "a", encoding="utf-8") as f:
        # WAL is still received immediately; only applying it waits.
        f.write(f"recovery_min_apply_delay = '{DELAY_S}s'\n")


def ensure_source() -> None:
    bootstrap_source()
    if not is_running(SOURCE):
        start(SOURCE, SOURCE_PORT)


def source_status() -> dict:
    with connect(SOURCE_PORT) as conn:
        replay_ts, replay_lsn, receive_lsn, paused = conn.execute(
            "SELECT pg_last_xact_replay_timestamp(), pg_last_wal_replay_lsn()::text,"
            " pg_last_wal_receive_lsn()::text, pg_get_wal_replay_pause_state()"
        ).fetchone()
    return {"replay_timestamp": replay_ts, "replay_lsn": replay_lsn, "receive_lsn": receive_lsn,
            "pause_state": paused, "configured_delay_s": DELAY_S}


def freeze_base() -> tuple[datetime, str, float]:
    """Captures the source's current state as `base`. Returns the production
    commit time it corresponds to (T0), its WAL position, and the copy time."""
    with connect(SOURCE_PORT) as conn:
        conn.execute("SELECT pg_wal_replay_pause()")
        while conn.execute("SELECT pg_get_wal_replay_pause_state()").fetchone()[0] != "paused":
            time.sleep(0.05)
        t0, lsn, has_unapplied_wal = conn.execute(
            "SELECT pg_last_xact_replay_timestamp(), pg_last_wal_replay_lsn()::text,"
            " pg_last_wal_receive_lsn() > pg_last_wal_replay_lsn()").fetchone()
        if t0 is None:
            # Refusing must not leave replay paused: a paused source never reaches
            # the transaction that would give it a T0, and stops following production.
            conn.execute("SELECT pg_wal_replay_resume()")
            raise RuntimeError("the twin source has not replayed any transaction yet")
    stop(SOURCE)
    try:
        seconds = copy(SOURCE, BASE)
    finally:
        start(SOURCE, SOURCE_PORT)  # replay resumes; the pause does not survive a restart
    # The copy holds WAL received but not yet applied. Recovering it all would move
    # the clone to "now"; stopping at this position keeps it at T0.
    conf = "primary_conninfo = ''\nrecovery_min_apply_delay = 0\n"
    if has_unapplied_wal:
        # Not inclusive: a delayed standby waits at a commit record, so this position is
        # the start of the next commit. An inclusive target would apply that commit, and
        # the replay would then run the same transaction a second time.
        conf += (f"recovery_target_lsn = '{lsn}'\nrecovery_target_inclusive = false\n"
                 "recovery_target_action = 'promote'\n")
        (BASE / "promote_manually").unlink(missing_ok=True)
    else:
        # Production was idle: there is no later WAL for a recovery target to stop at,
        # and a standby with a target it never reaches waits forever. Promote it directly.
        (BASE / "promote_manually").touch()
    (BASE / "postgresql.auto.conf").write_text(conf, encoding="utf-8")
    return t0, lsn, seconds


def fresh_work(cpus: str | None = None) -> float:
    """A new standalone database at T0. Returns seconds spent preparing it."""
    started = time.monotonic()
    stop(WORK)
    copy(BASE, WORK)
    # Flush the copy to disk now, so the kernel is not writing it back during the measurement.
    _run("sync", "-f", str(WORK))
    start(WORK, WORK_PORT, cpus)
    if (WORK / "promote_manually").exists():
        _run("pg_ctl", "-D", str(WORK), "-w", "-t", "120", "promote")
    deadline = time.monotonic() + 300
    with connect(WORK_PORT) as conn:
        while conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            if time.monotonic() > deadline:
                raise RuntimeError("clone did not finish recovery")
            time.sleep(0.2)
    return time.monotonic() - started
