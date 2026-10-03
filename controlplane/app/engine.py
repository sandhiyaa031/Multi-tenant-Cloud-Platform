"""The verification engine and canary controller.

A worker that takes proposals through the lifecycle:

    PROPOSED -> VERIFYING -> (REJECTED | INCONCLUSIVE | AWAITING_APPROVAL | APPROVED)
    APPROVED -> CANARY -> (APPLIED | ROLLED_BACK)

Verification tiers, cheapest first:
    T0  static rules            (this process, milliseconds)
    T1  planner what-if         (twin source, seconds)
    T2  digital twin replay     (twin node, minutes) + the tenant-aware gate
    T3  canary on production    (minutes), enforcing the contract T2 produced

    python -m app.engine          # loop forever
    python -m app.engine --once   # process whatever is queued, then exit
"""
import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from dbpilot_core import actions, gate

log = logging.getLogger("dbpilot.engine")


@dataclass(frozen=True)
class Config:
    control_url: str
    twin_url: str
    twin_token: str
    executor_user: str
    executor_password: str
    pooler_admin_user: str
    pooler_admin_password: str
    twin_window_s: float = 120
    twin_repetitions: int = 2
    # An inconclusive verdict buys another replay, up to this many in total; then it escalates.
    twin_max_looks: int = 3
    canary_baseline_windows: int = 3
    canary_windows: int = 3
    canary_window_s: float = 60          # the collector's interval
    canary_min_txns: int = 5             # per key per window, below which the window is not judged
    cluster_memory_bytes: int = 6 * 1024**3
    canary_max_deadlocks: int = 3                       # new deadlocks per window
    canary_max_replica_lag_bytes: int = 256 * 1024**2   # WAL a standby has not yet received
    poll_s: float = 3

    @staticmethod
    def from_env() -> "Config":
        e = os.environ
        return Config(
            control_url=e["CONTROL_DB_ENGINE_URL"], twin_url=e.get("TWIN_URL", "http://twin:8080"),
            twin_token=e["TWIN_TOKEN"], executor_user=e.get("DP_EXECUTOR_USER", "dbpilot_executor"),
            executor_password=e["DP_EXECUTOR_PASSWORD"],
            pooler_admin_user=e.get("DP_POOLER_ADMIN_USER", "pgbouncer_auth"),
            pooler_admin_password=e["DP_POOLER_ADMIN_PASSWORD"],
            twin_window_s=float(e.get("TWIN_WINDOW_S", "120")), twin_repetitions=int(e.get("TWIN_REPETITIONS", "2")),
            twin_max_looks=int(e.get("TWIN_MAX_LOOKS", "3")),
            canary_baseline_windows=int(e.get("CANARY_BASELINE_WINDOWS", "3")),
            canary_windows=int(e.get("CANARY_WINDOWS", "3")),
            canary_window_s=float(e.get("COLLECT_INTERVAL_S", "60")),
            cluster_memory_bytes=int(e.get("CLUSTER_MEMORY_BYTES", str(6 * 1024**3))),
        )


# ── T0: static rules ─────────────────────────────────────────────────────────

def t0_static(action: actions.Action, facts: dict, memory_bytes: int) -> tuple[str, str]:
    """Returns (decision, summary). `facts`: tenant_roles, max_connections, pool_size, applied_actions."""
    if isinstance(action, actions.NoAction):
        return "SKIPPED", "no action proposed"
    if not isinstance(action, actions.EXECUTABLE):
        return "SKIPPED", f"{action.type} is advisory and needs a human"
    role = actions.target_tenant(action)
    if role is not None and role not in facts["tenant_roles"]:
        return "REJECT", f"{role} is not a tenant of this cluster"
    if action.model_dump() in facts["applied_actions"]:
        return "REJECT", "an identical action is already applied"

    # Worst-case memory: every connection running a sort at once. A setting that could
    # exhaust RAM under full concurrency is refused whatever it does for latency.
    budget_kb = memory_bytes * 0.25 / 1024
    if getattr(action, "name", None) == "work_mem":
        value_kb = int(action.value.removesuffix("kB"))
        connections = facts["max_connections"] if isinstance(action, actions.InstanceSetting) else facts["pool_size"]
        if value_kb * connections > budget_kb:
            return "REJECT", (f"work_mem {value_kb // 1024} MB x {connections} connections exceeds a quarter "
                              f"of the instance's memory")
    return "APPROVE", "within static limits"


# ── T3: canary decisions (pure functions) ────────────────────────────────────

def window_breaches(baseline: dict[str, float], observed: dict[str, tuple[float, int]],
                    contract: dict[str, float], min_txns: int) -> list[str]:
    """Keys whose p95 in this window exceeds what the contract allows.
    `observed`: key -> (p95 ms, transaction count)."""
    breaches = []
    for key, limit in contract.items():
        if key not in baseline or key not in observed:
            continue
        p95, count = observed[key]
        if count >= min_txns and p95 / baseline[key] > limit:
            breaches.append(f"{key}: p95 {p95 / baseline[key]:.2f}x baseline, contract allows {limit:.2f}x")
    return breaches


def rollback_reason(history: list[dict]) -> str | None:
    """`history`: one entry per canary window: {"breaches": [...], "telemetry": bool}."""
    last3 = history[-3:]
    if len(history) >= 2 and not history[-1]["telemetry"] and not history[-2]["telemetry"]:
        return "telemetry lost for two consecutive windows; cannot confirm safety"
    breached = [h for h in last3 if h["breaches"]]
    if len(breached) >= 2:
        return "contract breached in 2 of the last 3 windows: " + "; ".join(breached[-1]["breaches"])
    return None


def guard_breaches(deadlocks: int, replica_lag_bytes: float, config: "Config") -> list[str]:
    """Signs of harm that latency percentiles do not show, checked once per canary window."""
    breaches = []
    if deadlocks > config.canary_max_deadlocks:
        breaches.append(f"{deadlocks} deadlocks in one window, limit {config.canary_max_deadlocks}")
    if replica_lag_bytes > config.canary_max_replica_lag_bytes:
        breaches.append(f"replica is {replica_lag_bytes / 1024**2:.0f} MB behind, "
                        f"limit {config.canary_max_replica_lag_bytes / 1024**2:.0f} MB")
    return breaches


def canary_stages(action: actions.Action, plan: actions.Plan, twin_effects: dict | None) -> list[tuple[str, list[str]]]:
    """How a change is exposed to production: [(label, statements), ...].

    An index for every tenant is built on one partition first and on the others
    only after that partition's canary held. The first partition is the tenant the
    twin predicted to gain most (the first tenant, without a prediction). Every
    other action is a single stage: applied, observed for a fixed time, then kept
    or undone.
    """
    if not (isinstance(action, actions.CreateIndex) and len(plan.apply) > 1):
        return [("all at once", plan.apply)]
    first = 0
    if twin_effects:
        best = None
        for i, statement in enumerate(plan.apply):
            relation = statement.split(" ON ")[1].split(" ")[0]              # ch.order_line_t_analytic
            role = relation.split(".", 1)[1].removeprefix(action.table + "_")
            ratios = [e["ratio"] for k, e in twin_effects.items() if k.startswith(role + "/") and e.get("ratio")]
            if ratios and (best is None or min(ratios) < best):
                best, first = min(ratios), i
    rest = [s for i, s in enumerate(plan.apply) if i != first]
    return [("stage 1: one tenant's partition", [plan.apply[first]]), ("stage 2: remaining partitions", rest)]


# ── The engine ───────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, config: Config):
        self.c = config
        self.http = httpx.Client(base_url=config.twin_url, timeout=60,
                                 headers={"Authorization": f"Bearer {config.twin_token}"})

    def control(self) -> psycopg.Connection:
        return psycopg.connect(self.c.control_url, row_factory=dict_row, autocommit=True)

    def production(self, cluster: dict) -> psycopg.Connection:
        return psycopg.connect(
            host=cluster["primary_host"], port=cluster["primary_port"] or 5432, dbname=cluster["database_name"],
            user=self.c.executor_user, password=self.c.executor_password, autocommit=True, connect_timeout=10)

    # -- queue ---------------------------------------------------------------

    def claim(self, db: psycopg.Connection, from_state: str, to_state: str) -> dict | None:
        """SKIP LOCKED lets several engines share the queue without ever taking the same proposal."""
        try:
            return db.execute(
                # Only clusters the engine can actually reach (a primary address is registered).
                "UPDATE cp.proposals SET state = %s WHERE id = ("
                "  SELECT p.id FROM cp.proposals p JOIN cp.clusters c ON c.id = p.cluster_id"
                "  WHERE p.state = %s AND c.primary_host IS NOT NULL ORDER BY p.created_at"
                "  FOR UPDATE OF p SKIP LOCKED LIMIT 1) RETURNING *",
                (to_state, from_state),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            return None  # another change is already in canary on that cluster; try again later

    def set_state(self, db, proposal: dict, state: str, reason: str) -> None:
        db.execute("UPDATE cp.proposals SET state = %s, state_reason = %s WHERE id = %s",
                   (state, reason[:2000], proposal["id"]))
        log.info("proposal %s -> %s: %s", proposal["id"], state, reason)

    def step(self, db, proposal: dict, tier: str, decision: str, summary: str, detail: dict, seconds: float) -> None:
        db.execute(
            "INSERT INTO cp.verification_steps (proposal_id, org_id, tier, decision, summary, detail, seconds)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (proposal["id"], proposal["org_id"], tier, decision, summary[:2000], Jsonb(detail), seconds))

    # -- verification --------------------------------------------------------

    def facts(self, db, cluster: dict) -> dict:
        tenants = db.execute("SELECT id, db_role FROM cp.tenants WHERE cluster_id = %s", (cluster["id"],)).fetchall()
        applied = db.execute(
            "SELECT action FROM cp.proposals WHERE cluster_id = %s AND state IN ('APPLIED', 'CANARY')",
            (cluster["id"],)).fetchall()
        return {"tenant_roles": {t["db_role"] for t in tenants}, "tenant_ids": {t["db_role"]: t["id"] for t in tenants},
                "max_connections": 200, "pool_size": 20,
                "applied_actions": [actions.parse_action(a["action"]).model_dump() for a in applied]}

    def verify(self, db, proposal: dict) -> None:
        cluster = db.execute("SELECT * FROM cp.clusters WHERE id = %s", (proposal["cluster_id"],)).fetchone()
        try:
            action = actions.parse_action(proposal["action"])
        except ValueError as exc:
            self.step(db, proposal, "T0", "REJECT", "not a valid action", {"error": str(exc)[:1000]}, 0)
            return self.set_state(db, proposal, "REJECTED", "T0: not a valid action")

        started = time.monotonic()
        decision, summary = t0_static(action, self.facts(db, cluster), self.c.cluster_memory_bytes)
        self.step(db, proposal, "T0", decision, summary, {}, time.monotonic() - started)
        if decision == "SKIPPED":
            return self.set_state(db, proposal, "ADVISORY", summary)
        if decision == "REJECT":
            return self.set_state(db, proposal, "REJECTED", f"T0: {summary}")

        if proposal["verification"] != "full":
            for tier in ("T1", "T2"):
                self.step(db, proposal, tier, "SKIPPED", f"verification mode is {proposal['verification']}", {}, 0)
            return self.set_state(db, proposal, "APPROVED", f"verification mode {proposal['verification']}: twin skipped")

        # T1
        if isinstance(action, actions.CreateIndex):
            started = time.monotonic()
            queries = [r["query"] for r in db.execute(
                "SELECT f.query FROM cp.query_fingerprints f WHERE f.cluster_id = %s AND f.query ILIKE %s"
                " AND f.query NOT ILIKE 'EXPLAIN%%' LIMIT 100",
                (cluster["id"], f"%ch.{action.table}%"))]
            try:
                result = self.http.post("/whatif", json={"action": proposal["action"], "queries": queries}).raise_for_status().json()
            except httpx.HTTPError as exc:
                # T1 is a cheap screen, not a judge: if it cannot run, the replay still decides.
                result = None
                self.step(db, proposal, "T1", "SKIPPED", "planner what-if unavailable; continuing to the twin replay",
                          {"error": str(exc)[:500]}, time.monotonic() - started)
            if result is None:
                pass
            elif result["explained"] > 0 and result["improved"] == 0:
                self.step(db, proposal, "T1", "REJECT", "the planner would not use this index for any observed query",
                          result, time.monotonic() - started)
                return self.set_state(db, proposal, "REJECTED", "T1: planner would not use the index")
            else:
                self.step(db, proposal, "T1", "APPROVE",
                          f"planner cost improves for {result['improved']} of {result['explained']} observed queries",
                          result, time.monotonic() - started)
        else:
            self.step(db, proposal, "T1", "SKIPPED", "planner what-if applies to indexes only", {}, 0)

        # T2
        # An interval that straddles a threshold buys another replay of a fresh window,
        # judged together with the earlier ones, until the verdict is firm or the budget
        # is spent. Looking several times is paid for in judge(): each look is tested at
        # a stricter level, so the chance of a false "safe" over all looks stays at 5%.
        started = time.monotonic()
        pooled = {"control": {}, "treatment": {}, "wal_control": 0, "wal_treatment": 0, "errors": 0,
                  "transactions": 0, "repetitions": 0, "window_s": 0.0}
        verdict, detail, note = None, {}, ""
        for look in range(1, self.c.twin_max_looks + 1):
            try:
                # With one repetition per replay, the arm that runs first alternates between replays.
                run = self.twin_run(proposal["action"], treatment_first=look % 2 == 0)
            except Exception as exc:
                if verdict is None:
                    self.step(db, proposal, "T2", "INCONCLUSIVE", "twin run failed", {"error": str(exc)[:1000]},
                              time.monotonic() - started)
                    return self.set_state(db, proposal, "INCONCLUSIVE", f"T2: twin run failed: {exc}")
                note = f" (replay {look} failed: {exc})"
                break
            control, treatment = run["arms"]["control"], run["arms"]["treatment"]
            warmup = min(15.0, run["window_s"] * 0.15)
            for arm in ("control", "treatment"):
                gate.pool(pooled[arm], {tuple(k.split("/")): [tuple(s) for s in v]
                                        for k, v in run["arms"][arm]["samples"].items()}, look, warmup)
            pooled["wal_control"] += control["wal_bytes"]
            pooled["wal_treatment"] += treatment["wal_bytes"]
            pooled["errors"] += sum(treatment["errors"].values()) + sum(control["errors"].values())
            pooled["transactions"] += run["transactions"]
            pooled["repetitions"] += run["repetitions"]
            pooled["window_s"] = run["window_s"]
            pooled["treatment_arm"] = treatment
            verdict, detail = self.judge(db, proposal, cluster, action, pooled)
            detail["looks"] = look
            if verdict.decision != "INCONCLUSIVE":
                break
            log.info("proposal %s inconclusive after replay %d of %d", proposal["id"], look, self.c.twin_max_looks)
        seconds = time.monotonic() - started
        treatment = pooled["treatment_arm"]
        db.execute(
            "INSERT INTO cp.twin_runs (proposal_id, org_id, window_s, transactions, repetitions, replay_errors,"
            " wal_ratio, storage_delta_bytes, apply_seconds, verdict, seconds)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (proposal["id"], proposal["org_id"], pooled["window_s"], pooled["transactions"], pooled["repetitions"],
             pooled["errors"], pooled["wal_treatment"] / pooled["wal_control"] if pooled["wal_control"] else None,
             treatment.get("storage_delta_bytes", 0), treatment.get("apply_s"), Jsonb(detail), seconds))
        summary = "; ".join(verdict.reasons) + f" [{detail['looks']} replay(s)]" + note
        self.step(db, proposal, "T2", verdict.decision, summary, detail, seconds)

        if verdict.decision == "REJECT":
            self.set_state(db, proposal, "REJECTED", "T2: " + "; ".join(verdict.reasons))
        elif verdict.decision == "INCONCLUSIVE":
            self.set_state(db, proposal, "INCONCLUSIVE", "T2: " + "; ".join(verdict.reasons))
        elif proposal["auto_approve"]:
            self.set_state(db, proposal, "APPROVED", "T2: " + "; ".join(verdict.reasons))
        else:
            self.set_state(db, proposal, "AWAITING_APPROVAL", "T2: " + "; ".join(verdict.reasons))

    def twin_run(self, action: dict, treatment_first: bool = False) -> dict:
        body = {"action": action, "window_s": self.c.twin_window_s, "repetitions": self.c.twin_repetitions,
                "treatment_first": treatment_first}
        run_id = self.http.post("/runs", json=body).raise_for_status().json()["run_id"]
        deadline = time.monotonic() + self.c.twin_window_s * self.c.twin_repetitions * 2 + 900
        while time.monotonic() < deadline:
            time.sleep(5)
            state = self.http.get(f"/runs/{run_id}").raise_for_status().json()
            if state["state"] == "DONE":
                return state["result"]
            if state["state"] == "FAILED":
                raise RuntimeError(state["error"])
        raise TimeoutError("twin run did not finish")

    def calibration(self, db, cluster: dict, action_type: str, default: float) -> tuple[float, int]:
        """Canary tolerance for this kind of action, from how far the twin has been off before."""
        rows = db.execute(
            "SELECT twin_effects, production_ratios FROM cp.outcome_ledger WHERE cluster_id = %s AND action_type = %s"
            " AND verification = 'full' AND twin_effects IS NOT NULL AND production_ratios IS NOT NULL",
            (cluster["id"], action_type)).fetchall()
        pairs = [(effect.get("ratio"), r["production_ratios"][key]) for r in rows
                 for key, effect in r["twin_effects"].items() if key in r["production_ratios"]]
        return gate.calibrated_tolerance(pairs, default)

    def judge(self, db, proposal: dict, cluster: dict, action, pooled: dict) -> tuple[gate.Verdict, dict]:
        """The verdict in the proposal's gate mode, and what is stored about it: the
        verdict itself, the other gate mode's verdict on the same measurements (for
        comparison only; it decides nothing), and the calibration used."""
        slos = {(r["db_role"], r["query_class"]): (r["percentile"], float(r["threshold_ms"])) for r in db.execute(
            "SELECT t.db_role, s.query_class, s.percentile, s.threshold_ms FROM cp.slos s"
            " JOIN cp.tenants t ON t.id = s.tenant_id WHERE t.cluster_id = %s", (cluster["id"],))}
        base = gate.GatePolicy()
        tolerance, history = self.calibration(db, cluster, action.type, base.contract_tolerance)
        policy = gate.GatePolicy(confidence=1 - (1 - base.confidence) / self.c.twin_max_looks, n_boot=4000,
                                 contract_tolerance=tolerance)
        treatment = pooled["treatment_arm"]

        def decide(mode: str) -> gate.Verdict:
            return gate.decide(
                pooled["control"], pooled["treatment"], actions.target_tenant(action), policy,
                mode=mode, cheap_to_undo=treatment.get("cheap_to_undo", True),
                wal_ratio=pooled["wal_treatment"] / pooled["wal_control"] if pooled["wal_control"] else None,
                storage_delta_bytes=treatment.get("storage_delta_bytes", 0), slo_ms=slos)

        verdict = decide(proposal["gate_mode"])
        other = decide("aggregate" if proposal["gate_mode"] == "per_tenant" else "per_tenant")
        detail = verdict.to_dict()
        detail["shadow"] = {"mode": other.mode, "decision": other.decision, "reasons": other.reasons,
                            "effects": other.effects}
        detail["calibration"] = {"contract_tolerance": round(tolerance, 3), "history_pairs": history}
        return verdict, detail

    # -- canary --------------------------------------------------------------

    def latency_windows(self, db, cluster_id, since: datetime) -> list[dict]:
        """Per collector window since `since`: {"end": ts, "keys": {"role/CLASS": (p95, count)}}."""
        rows = db.execute(
            "SELECT l.window_end, t.db_role, l.query_class, l.p95_ms, l.txn_count FROM cp.latency_stats l"
            " JOIN cp.tenants t ON t.id = l.tenant_id WHERE l.cluster_id = %s AND l.window_end > %s"
            " ORDER BY l.window_end", (cluster_id, since)).fetchall()
        windows: dict[datetime, dict] = {}
        for r in rows:
            windows.setdefault(r["window_end"], {})[f"{r['db_role']}/{r['query_class']}"] = (r["p95_ms"], r["txn_count"])
        return [{"end": end, "keys": keys} for end, keys in sorted(windows.items())]

    @staticmethod
    def health(prod: psycopg.Connection) -> tuple[int, float]:
        """(deadlocks so far in this database, bytes of WAL the furthest-behind standby has not received)."""
        return prod.execute(
            "SELECT (SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()),"
            " (SELECT coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), flush_lsn)), 0) FROM pg_stat_replication)"
        ).fetchone()

    def reconnect_pooler(self, cluster: dict) -> None:
        """Role-level changes apply to new server connections; ask the pooler to recycle its own."""
        try:
            with psycopg.connect(host=cluster["pooler_host"], port=cluster["pooler_port"], dbname="pgbouncer",
                                 user=self.c.pooler_admin_user, password=self.c.pooler_admin_password,
                                 autocommit=True, connect_timeout=5) as admin:
                admin.execute("RECONNECT")
        except psycopg.Error as exc:
            log.warning("pooler reconnect failed: %s", exc)

    def canary(self, db, proposal: dict) -> None:
        cluster = db.execute("SELECT * FROM cp.clusters WHERE id = %s", (proposal["cluster_id"],)).fetchone()
        action = actions.parse_action(proposal["action"])
        observe_only = proposal["verification"] == "none"

        # Baseline: what each tenant's p95 looked like just before the change.
        now = datetime.now(timezone.utc)
        lookback = now.timestamp() - self.c.canary_window_s * (self.c.canary_baseline_windows + 0.5)
        before = self.latency_windows(db, cluster["id"], datetime.fromtimestamp(lookback, tz=timezone.utc))
        baseline: dict[str, float] = {}
        for key in {k for w in before for k in w["keys"]}:
            values = [w["keys"][key][0] for w in before if key in w["keys"] and w["keys"][key][1] >= self.c.canary_min_txns]
            if values:
                baseline[key] = sum(values) / len(values)
        if not baseline:
            return self.set_state(db, proposal, "FAILED", "T3: no latency baseline; is the collector running and is there load?")

        twin = db.execute("SELECT verdict FROM cp.twin_runs WHERE proposal_id = %s ORDER BY created_at DESC LIMIT 1",
                          (proposal["id"],)).fetchone()
        policy = gate.GatePolicy()
        default_limit = round(1 + policy.max_regression + policy.contract_tolerance, 3)
        contract = {key: default_limit for key in baseline}
        if twin:
            contract.update({k: v for k, v in twin["verdict"].get("contract", {}).items() if k in baseline})

        try:
            with self.production(cluster) as prod:
                plan = actions.plan(action, prod)
        except (psycopg.Error, actions.ActionRefused) as exc:
            return self.set_state(db, proposal, "FAILED", f"T3: could not apply: {exc}")
        # Without verification there is no canary to stage: the change goes out whole.
        stages = ([("all at once", plan.apply)] if observe_only
                  else canary_stages(action, plan, twin["verdict"].get("effects") if twin else None))

        history: list[dict] = []
        applied: list[str] = []
        reason = None
        for number, (label, statements) in enumerate(stages, start=1):
            try:
                with self.production(cluster) as prod:
                    deadlocks_before = self.health(prod)[0]
                    actions.run(statements, prod)
            except psycopg.Error as exc:
                if not applied:
                    return self.set_state(db, proposal, "FAILED", f"T3: could not apply: {exc}")
                reason = f"{label} could not be applied: {exc}"
                break
            applied += statements
            if isinstance(action, (actions.RoleSetting, actions.ConcurrencyCap)):
                self.reconnect_pooler(cluster)
            applied_at = datetime.now(timezone.utc)
            if number == 1:
                db.execute(
                    "INSERT INTO cp.canaries (proposal_id, org_id, applied, inverse, contract, baseline)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (proposal["id"], proposal["org_id"], Jsonb(applied), Jsonb(plan.inverse), Jsonb(contract), Jsonb(baseline)))
            else:
                db.execute("UPDATE cp.canaries SET applied = %s WHERE proposal_id = %s", (Jsonb(applied), proposal["id"]))
            log.info("proposal %s: %s applied to production; observing", proposal["id"], label)

            stage_history: list[dict] = []
            seen: set[datetime] = set()
            deadline = time.monotonic() + self.c.canary_window_s * (self.c.canary_windows + 3)
            while len(stage_history) < self.c.canary_windows:
                time.sleep(min(5.0, self.c.canary_window_s / 4))
                fresh = [w for w in self.latency_windows(db, cluster["id"], applied_at) if w["end"] not in seen
                         # A window that began before the change mixes old and new behaviour; skip it.
                         and (w["end"] - applied_at).total_seconds() >= self.c.canary_window_s * 0.9]
                for w in fresh:
                    seen.add(w["end"])
                    breaches = window_breaches(baseline, w["keys"], contract, self.c.canary_min_txns)
                    try:
                        with self.production(cluster) as prod:
                            deadlocks, lag = self.health(prod)
                        breaches += guard_breaches(deadlocks - deadlocks_before, lag, self.c)
                        deadlocks_before = deadlocks
                    except psycopg.Error as exc:
                        log.warning("health check failed: %s", exc)
                    stage_history.append({
                        "end": w["end"].isoformat(), "telemetry": True, "stage": number, "stage_label": label,
                        "ratios": {k: round(v[0] / baseline[k], 3) for k, v in w["keys"].items() if k in baseline},
                        "counts": {k: v[1] for k, v in w["keys"].items()}, "breaches": breaches})
                if time.monotonic() > deadline and len(stage_history) < self.c.canary_windows:
                    stage_history.append({"end": datetime.now(timezone.utc).isoformat(), "telemetry": False,
                                          "stage": number, "stage_label": label, "ratios": {}, "counts": {}, "breaches": []})
                    stage_history.append(dict(stage_history[-1]))
                db.execute("UPDATE cp.canaries SET observations = %s WHERE proposal_id = %s",
                           (Jsonb(history + stage_history), proposal["id"]))
                reason = None if observe_only else rollback_reason(stage_history)
                if reason:
                    break
            history += stage_history
            if reason:
                if len(stages) > 1:
                    reason = f"{label}: {reason}"
                break

        # Production result for the ledger: mean observed ratio per tenant and class.
        result = {}
        for key in baseline:
            ratios = [h["ratios"][key] for h in history if key in h["ratios"] and h["counts"].get(key, 0) >= self.c.canary_min_txns]
            if ratios:
                result[key] = round(sum(ratios) / len(ratios), 3)

        if reason:
            try:
                with self.production(cluster) as prod:
                    actions.run(plan.inverse, prod)
                if isinstance(action, (actions.RoleSetting, actions.ConcurrencyCap)):
                    self.reconnect_pooler(cluster)
            except psycopg.Error as exc:
                reason += f" (ROLLBACK FAILED: {exc})"
            db.execute("UPDATE cp.canaries SET outcome = 'ROLLED_BACK', outcome_reason = %s, result = %s,"
                       " finished_at = now() WHERE proposal_id = %s", (reason, Jsonb(result), proposal["id"]))
            self.step(db, proposal, "T3", "REJECT", reason, {"observations": history}, 0)
            return self.set_state(db, proposal, "ROLLED_BACK", "T3: " + reason)

        summary = ("observed without enforcement" if observe_only else
                   "contract held for every tenant" + (f" in each of {len(stages)} stages" if len(stages) > 1 else ""))
        db.execute("UPDATE cp.canaries SET outcome = 'HELD', outcome_reason = %s, result = %s, finished_at = now()"
                   " WHERE proposal_id = %s", (summary, Jsonb(result), proposal["id"]))
        self.step(db, proposal, "T3", "SKIPPED" if observe_only else "APPROVE", summary, {"observations": history}, 0)
        self.set_state(db, proposal, "APPLIED", "T3: " + summary)

    def manual_rollback(self, db) -> bool:
        """Undoes an applied change a member asked to roll back, using the stored inverse."""
        with db.transaction():
            proposal = db.execute(
                "SELECT p.* FROM cp.proposals p JOIN cp.clusters c ON c.id = p.cluster_id"
                " WHERE p.state = 'ROLLBACK_REQUESTED' AND c.primary_host IS NOT NULL ORDER BY p.updated_at"
                " FOR UPDATE OF p SKIP LOCKED LIMIT 1").fetchone()
            if proposal is None:
                return False
            cluster = db.execute("SELECT * FROM cp.clusters WHERE id = %s", (proposal["cluster_id"],)).fetchone()
            canary = db.execute("SELECT inverse FROM cp.canaries WHERE proposal_id = %s", (proposal["id"],)).fetchone()
            try:
                with self.production(cluster) as prod:
                    actions.run(canary["inverse"], prod)
                self.reconnect_pooler(cluster)
                self.set_state(db, proposal, "ROLLED_BACK", "rolled back on request: " + proposal["state_reason"])
            except psycopg.Error as exc:
                self.set_state(db, proposal, "FAILED", f"rollback failed: {exc}")
        return True

    def recover(self) -> None:
        """Run at start-up. A proposal left mid-flight means the previous engine died.

        A half-finished verification is simply marked failed. A canary is different:
        the change is live in production and nothing has been watching it, so it is
        rolled back. Unobserved means unsafe.
        """
        with self.control() as db:
            for proposal in db.execute(
                "SELECT p.* FROM cp.proposals p JOIN cp.clusters c ON c.id = p.cluster_id"
                " WHERE p.state IN ('VERIFYING', 'CANARY') AND c.primary_host IS NOT NULL").fetchall():
                if proposal["state"] == "VERIFYING":
                    self.set_state(db, proposal, "FAILED", "engine restarted during verification")
                    continue
                cluster = db.execute("SELECT * FROM cp.clusters WHERE id = %s", (proposal["cluster_id"],)).fetchone()
                canary = db.execute("SELECT inverse FROM cp.canaries WHERE proposal_id = %s", (proposal["id"],)).fetchone()
                reason = "engine restarted during canary; rolled back because the change was unobserved"
                try:
                    if canary:
                        with self.production(cluster) as prod:
                            actions.run(canary["inverse"], prod)
                        self.reconnect_pooler(cluster)
                        db.execute("UPDATE cp.canaries SET outcome = 'ROLLED_BACK', outcome_reason = %s,"
                                   " finished_at = now() WHERE proposal_id = %s", (reason, proposal["id"]))
                    self.set_state(db, proposal, "ROLLED_BACK", reason)
                except psycopg.Error as exc:
                    self.set_state(db, proposal, "FAILED", f"{reason}; ROLLBACK FAILED: {exc}")

    # -- loop ----------------------------------------------------------------

    def work_once(self) -> bool:
        """Processes at most one proposal. Returns whether there was anything to do."""
        with self.control() as db:
            proposal = self.claim(db, "APPROVED", "CANARY")
            if proposal:
                try:
                    self.canary(db, proposal)
                except Exception as exc:
                    log.exception("canary crashed")
                    self.set_state(db, proposal, "FAILED", f"T3: engine error: {exc}")
                return True
            if self.manual_rollback(db):
                return True
            proposal = self.claim(db, "PROPOSED", "VERIFYING")
            if proposal:
                try:
                    self.verify(db, proposal)
                except Exception as exc:
                    log.exception("verification crashed")
                    self.set_state(db, proposal, "FAILED", f"engine error: {exc}")
                return True
        return False


def main() -> None:
    parser = argparse.ArgumentParser(prog="engine")
    parser.add_argument("--once", action="store_true", help="drain the queue, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    engine = Engine(Config.from_env())
    engine.recover()
    while True:
        try:
            worked = engine.work_once()
        except psycopg.OperationalError:
            log.exception("control-plane database unavailable; will retry")
            worked = False
        if not worked:
            if args.once:
                return
            time.sleep(engine.c.poll_s)


if __name__ == "__main__":
    main()
