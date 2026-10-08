"""Reads PostgreSQL's JSON statement log and reassembles it into transactions.

One capture serves two consumers:

  * telemetry - how long each tenant's transactions took (latency percentiles),
    which cumulative views like pg_stat_statements cannot give;
  * the digital twin - the exact statements, parameters, timing and tenant role
    needed to replay a window of production workload on a clone.

Statements are grouped by backend process id. Behind a transaction-mode pooler a
backend serves many clients, but never two transactions at once, so a
BEGIN..COMMIT sequence on one pid is one client transaction.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

_MESSAGE = re.compile(r"^duration: ([\d.]+) ms(?:  (statement|execute|parse|bind) ?([^:]*): (.*))?$", re.S)
_PARAM = re.compile(r"\$(\d+) = (NULL|'(?:[^']|'')*')")
_PLACEHOLDER = re.compile(r"\$(\d+)")
_NUMERIC = re.compile(r"^'(-?\d+(?:\.\d+)?)'$")
_END = {"COMMIT", "ROLLBACK", "END", "ABORT"}
# A transaction-mode pooler relabels a server connection with this statement before handing it
# to a client whose application_name differs. It is the pooler's, not a tenant's.
_POOLER_SET = re.compile(r"^SET\s+application_name\s*(?:=|TO)\s*'[^']*'\s*;?$", re.I)


@dataclass
class Statement:
    sql: str
    params: dict[int, str]  # placeholder number -> SQL literal ('text' or NULL)
    duration_ms: float
    query_id: int | None

    def literal_sql(self) -> str:
        """The statement with its parameters written in as literals, ready to re-execute.

        The log records parameter values but not their types. Client drivers
        bind numbers as numeric types, so a value that looks like a number is
        written as a bare numeric literal and everything else as a quoted one.
        This is an approximation: a text parameter consisting only of digits
        would be re-typed. It is one of the measured sources of replay error.
        """
        def literal(match: re.Match) -> str:
            value = self.params.get(int(match.group(1)))
            if value is None:
                return match.group(0)
            numeric = _NUMERIC.match(value)
            return numeric.group(1) if numeric else value

        return _PLACEHOLDER.sub(literal, self.sql)


@dataclass
class Transaction:
    user: str
    app: str
    pid: int
    start: datetime
    end: datetime
    statements: list[Statement] = field(default_factory=list)
    failed: bool = False

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start).total_seconds() * 1000

    @property
    def query_class(self) -> str:
        """Tenants label their connections with application_name ('oltp' / 'olap');
        anything unlabelled is treated as OLTP."""
        return "OLAP" if self.app.lower().startswith("olap") else "OLTP"


def _parse_ts(value: str) -> datetime:
    # "2026-10-03 11:00:00.123 UTC"
    return datetime.strptime(value[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)


class Assembler:
    """Feeds on log records in order and yields completed transactions."""

    def __init__(self) -> None:
        self._open: dict[int, Transaction] = {}
        self._pending_ms: dict[int, float] = {}  # parse/bind time awaiting its execute

    def feed(self, record: dict) -> Transaction | None:
        pid = record.get("pid")
        if record.get("backend_type") != "client backend" or pid is None:
            return None
        if record.get("error_severity") == "ERROR":
            txn = self._open.get(pid)
            if txn:
                txn.failed = True
            return None
        match = _MESSAGE.match(record.get("message", ""))
        if not match or match.group(2) is None:
            return None
        duration, phase, sql = float(match.group(1)), match.group(2), match.group(4).strip()
        if phase in ("parse", "bind"):
            self._pending_ms[pid] = self._pending_ms.get(pid, 0.0) + duration
            return None
        duration += self._pending_ms.pop(pid, 0.0)

        end = _parse_ts(record["timestamp"])
        start = end - timedelta(milliseconds=duration)
        params = {int(n): v for n, v in _PARAM.findall(record.get("detail") or "")}
        statement = Statement(sql=sql, params=params, duration_ms=duration, query_id=record.get("query_id") or None)
        verb = sql.split(None, 1)[0].upper().rstrip(";") if sql else ""

        txn = self._open.get(pid)
        if txn is None:
            txn = Transaction(user=record.get("user", ""), app=record.get("application_name") or "", pid=pid,
                              start=start, end=end)
            if verb in ("BEGIN", "START"):
                self._open[pid] = txn
                return None
            if _POOLER_SET.match(sql):
                return None
            # A statement outside BEGIN..COMMIT is its own transaction.
            txn.statements.append(statement)
            return txn
        if verb in _END:
            txn.end = end
            txn.failed = txn.failed or verb in ("ROLLBACK", "ABORT")
            del self._open[pid]
            return txn
        txn.statements.append(statement)
        txn.end = end
        return None


def read_records(path: Path, offset: int = 0) -> Iterator[tuple[dict, int]]:
    """Yields (record, offset after it). Stops at a partial last line, which is still being written."""
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line or not line.endswith(b"\n"):
                return
            offset += len(line)
            try:
                yield json.loads(line), offset
            except json.JSONDecodeError:
                continue


class LogTailer:
    """Follows a rotating log directory, returning transactions completed since the last call."""

    def __init__(self, directory: str):
        self.directory = Path(directory)
        self._offsets: dict[str, tuple[bytes, int]] = {}  # file name -> (first bytes, offset)
        self._assembler = Assembler()

    def read_new(self) -> list[Transaction]:
        completed: list[Transaction] = []
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in files:
            size = path.stat().st_size
            with open(path, "rb") as f:
                head = f.read(120)
            marker, offset = self._offsets.get(path.name, (head, 0))
            # The server reuses file names in a cycle and truncates on reuse: a
            # different beginning (or a shorter file) means it is a new file.
            if size < offset or marker != head[: len(marker)]:
                offset = 0
            marker = head
            if size == offset:
                continue
            for record, offset in read_records(path, offset):
                txn = self._assembler.feed(record)
                if txn is not None:
                    completed.append(txn)
            self._offsets[path.name] = (marker, offset)
        return completed


def read_window(directory: str, start: datetime, end: datetime) -> list[Transaction]:
    """All transactions that committed inside (start, end], oldest first."""
    assembler = Assembler()
    out: list[Transaction] = []
    for path in sorted(Path(directory).glob("*.json"), key=lambda p: p.stat().st_mtime):
        # Skip files last written before the window opened.
        if datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc) < start:
            continue
        for record, _ in read_records(path):
            txn = assembler.feed(record)
            if txn is not None and start < txn.end <= end:
                out.append(txn)
    out.sort(key=lambda t: t.start)
    return out
