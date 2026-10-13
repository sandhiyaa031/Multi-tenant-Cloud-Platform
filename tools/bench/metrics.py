"""Metrics shared by the benchmark scripts: percentiles per tenant and class, per operation of
the driver and per query template of the statement log, throughput, and the noise-floor
arithmetic of docs/evidence/bench_noise/protocol.md.

Pure functions over data already collected; nothing here talks to a database.
"""
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

MIN_TEMPLATE_N = 30          # a template with fewer executions in a segment has no percentile worth comparing

# First statement of each TPC-C transaction, as the driver sends it: tells the type of a logged transaction.
OLTP_TYPES = (("SELECT c_discount", "new_order"), ("UPDATE ch.warehouse", "payment"),
              ("DELETE FROM ch.new_order", "delivery"), ("SELECT d_next_o_id", "stock_level"),
              ("SELECT c_id FROM ch.customer", "order_status"), ("SELECT c_balance", "order_status"))


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    return sorted_values[max(1, math.ceil(p / 100 * len(sorted_values))) - 1]


def summary(values: list[float], seconds: float | None = None) -> dict:
    values = sorted(values)
    out = {"n": len(values)}
    if values:
        out.update(p50=percentile(values, 50), p95=percentile(values, 95), p99=percentile(values, 99),
                   mean=sum(values) / len(values))
    if seconds:
        out["per_s"] = len(values) / seconds
    return out


def load_ops(path: str | Path) -> list[list]:
    """The driver's --ops file: [scheduled wall time, tenant, class, operation, latency ms] per line."""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def client_metrics(ops: list[list], start: float, end: float) -> dict[str, dict]:
    """Per "tenant/CLASS" and per "tenant/CLASS/operation": percentiles and throughput of the
    operations scheduled in [start, end)."""
    groups: dict[str, list[float]] = defaultdict(list)
    for at, tenant, cls, op, latency in ops:
        if start <= at < end:
            groups[f"{tenant}/{cls}"].append(latency)
            groups[f"{tenant}/{cls}/{op}"].append(latency)
    return {key: summary(values, end - start) for key, values in sorted(groups.items())}


def transaction_type(txn) -> str:
    if txn.query_class == "OLAP":
        return "olap"
    first = txn.statements[0].sql.lstrip() if txn.statements else ""
    for prefix, name in OLTP_TYPES:
        if first.startswith(prefix):
            return name
    return "other"


def server_metrics(transactions: list, start: float, end: float,
                   tenants: set[str] | None = None) -> tuple[dict[str, dict], dict[str, str]]:
    """From logged transactions that started in [start, end), of `tenants` only when given (the
    log also holds the monitoring role's and the pooler's own statements).

    Returns ({key: summary}, {query id: sql text}). Keys:
      "tenant/CLASS"              the transaction's span inside the database (first statement to commit)
      "tenant/CLASS#<query id>"   one statement template's execution time
    """
    spans: dict[str, list[float]] = defaultdict(list)
    templates: dict[str, list[float]] = defaultdict(list)
    texts: dict[str, str] = {}
    for txn in transactions:
        at = txn.start.timestamp()
        if not start <= at < end or (tenants is not None and txn.user not in tenants):
            continue
        key = f"{txn.user}/{txn.query_class}"
        spans[key].append(txn.duration_ms)
        for statement in txn.statements:
            if statement.query_id is not None:
                templates[f"{key}#{statement.query_id}"].append(statement.duration_ms)
                texts.setdefault(str(statement.query_id), " ".join(statement.sql.split())[:140])
    out = {key: summary(values, end - start) for key, values in spans.items()}
    out.update({key: summary(values, end - start) for key, values in templates.items()})
    return dict(sorted(out.items())), texts


def db_time_share(ops: list[list], transactions: list, start: float, end: float) -> dict[str, dict]:
    """Per "tenant/CLASS" and per "tenant/CLASS/type": time spent executing statements in the
    database as a share of the latency the client saw, over [start, end)."""
    client: dict[str, list[float]] = defaultdict(list)
    for at, tenant, cls, op, latency in ops:
        if start <= at < end:
            client[f"{tenant}/{cls}"].append(latency)
            client[f"{tenant}/{cls}/{op if cls == 'OLTP' else 'olap'}"].append(latency)
    execute: dict[str, list[float]] = defaultdict(list)
    span: dict[str, list[float]] = defaultdict(list)
    statements: dict[str, list[int]] = defaultdict(list)
    tenants = {op[1] for op in ops}
    for txn in transactions:
        if not start <= txn.start.timestamp() < end or txn.user not in tenants:
            continue
        for key in (f"{txn.user}/{txn.query_class}", f"{txn.user}/{txn.query_class}/{transaction_type(txn)}"):
            execute[key].append(sum(s.duration_ms for s in txn.statements))
            span[key].append(txn.duration_ms)
            statements[key].append(len(txn.statements))
    out = {}
    for key in sorted(client):                      # what the driver ran; its set-up queries are not operations
        c, e, s = client.get(key, []), execute.get(key, []), span.get(key, [])
        row = {"client_n": len(c), "server_n": len(e)}
        if c and e:
            # Means, not sums: the two counts differ by the few transactions that straddle the window's edges.
            client_mean, exec_mean, span_mean = sum(c) / len(c), sum(e) / len(e), sum(s) / len(s)
            row.update(client_mean_ms=client_mean, exec_mean_ms=exec_mean, span_mean_ms=span_mean,
                       statements_per_txn=sum(statements[key]) / len(statements[key]),
                       db_time_share=exec_mean / client_mean, in_db_span_share=span_mean / client_mean)
        out[key] = row
    return out


# ── noise floor ──────────────────────────────────────────────────────────────

def _t(p: float, df: int) -> float:
    from dbpilot_core.gate import t_quantile
    return t_quantile(p, df)


def noise(log_ratios: list[float]) -> dict | None:
    """From ln(ratio) of repetitions between identical conditions: its standard deviation s, the
    95% interval of s, the mean with its 95% t interval, and MDE10: the smallest effect that 10
    repetitions detect with 80% power at a two-sided 5% level, (t(.975,9) + t(.80,9)) * s / sqrt(10)."""
    n = len(log_ratios)
    if n < 3:
        return None
    mean, s = statistics.fmean(log_ratios), statistics.stdev(log_ratios)
    half = _t(0.975, n - 1) * s / math.sqrt(n)
    mde = (_t(0.975, 9) + _t(0.80, 9)) * s / math.sqrt(10)
    # Interval of s from the chi-square distribution of (n-1) s^2 / sigma^2, by the Wilson-Hilferty approximation.
    def chi2(p: float) -> float:
        k = n - 1
        z = statistics.NormalDist().inv_cdf(p)
        return k * (1 - 2 / (9 * k) + z * math.sqrt(2 / (9 * k))) ** 3
    return {"n": n, "sd_log": s, "sd_log_lo": s * math.sqrt((n - 1) / chi2(0.975)), "sd_log_hi": s * math.sqrt((n - 1) / chi2(0.025)),
            "mean_ratio": math.exp(mean), "mean_lo": math.exp(mean - half), "mean_hi": math.exp(mean + half),
            "mde10_pct": (math.exp(mde) - 1) * 100}


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")
