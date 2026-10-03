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
    canary_baseline_windows: int = 3
    canary_windows: int = 3
    canary_window_s: float = 60          # the collector's interval
    canary_min_txns: int = 5             # per key per window, below which the window is not judged
    cluster_memory_bytes: int = 6 * 1024**3
    poll_s: float = 3

    @staticmethod
    def from_env() -> "Config":
        e = os.environ
        return Config(
            control_url=e["CONTROL_DB_ENGINE_URL"], twin_url=e.get("TWIN_URL", "http://twin:8080"),
            twin_token=e["TWIN_TOKEN"], executor_user=e.get("DP_EXECUTOR_USER", "postgres"),
            executor_password=e["DP_EXECUTOR_PASSWORD"],
            pooler_admin_user=e.get("DP_POOLER_ADMIN_USER", "pgbouncer_auth"),
            pooler_admin_password=e["DP_POOLER_ADMIN_PASSWORD"],
            twin_window_s=float(e.get("TWIN_WINDOW_S", "120")), twin_repetitions=int(e.get("TWIN_REPETITIONS", "2")),
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
            result = self.http.post("/whatif", json={"action": proposal["action"], "queries": queries}).raise_for_status().json()
            if result["explained"] > 0 and result["improved"] == 0:
                self.step(db, proposal, "T1", "REJECT", "the planner would not use this index for any observed query",
                          result, time.monotonic() - started)
                return self.set_state(db, proposal, "REJECTED", "T1: planner would not use the index")
            self.step(db, proposal, "T1", "APPROVE",
                      f"planner cost improves for {result['improved']} of {result['explained']} observed queries",
                      result, time.monotonic() - started)
        else:
            self.step(db, proposal, "T1", "SKIPPED", "planner what-if applies to indexes only", {}, 0)

        # T2
        started = time.monotonic()
        try:
            run = self.twin_run(proposal["action"])
        except Exception as exc:
            self.step(db, proposal, "T2", "INCONCLUSIVE", "twin run failed", {"error": str(exc)[:1000]},
                      time.monotonic() - started)
            return self.set_state(db, proposal, "INCONCLUSIVE", f"T2: twin run failed: {exc}")
        verdict = self.judge(db, proposal, cluster, action, run)
        seconds = time.monotonic() - started
        control, treatment = run["arms"]["control"], run["arms"]["treatment"]
        db.execute(
            "INSERT INTO cp.twin_runs (proposal_id, org_id, window_s, transactions, repetitions, replay_errors,"
            " wal_ratio, storage_delta_bytes, apply_seconds, verdict, seconds)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (proposal["id"], proposal["org_id"], run["window_s"], run["transactions"], run["repetitions"],
             sum(treatment["errors"].values()) + sum(control["errors"].values()),
             treatment["wal_bytes"] / control["wal_bytes"] if control["wal_bytes"] else None,
             treatment.get("storage_delta_bytes", 0), treatment.get("apply_s"), Jsonb(verdict.to_dict()), seconds))
        self.step(db, proposal, "T2", verdict.decision, "; ".join(verdict.reasons), verdict.to_dict(), seconds)

        if verdict.decision == "REJECT":
            self.set_state(db, proposal, "REJECTED", "T2: " + "; ".join(verdict.reasons))
        elif verdict.decision == "INCONCLUSIVE":
            self.set_state(db, proposal, "INCONCLUSIVE", "T2: " + "; ".join(verdict.reasons))
        elif proposal["auto_approve"]:
            self.set_state(db, proposal, "APPROVED", "T2: " + "; ".join(verdict.reasons))
        else:
            self.set_state(db, proposal, "AWAITING_APPROVAL", "T2: " + "; ".join(verdict.reasons))

    def twin_run(self, action: dict) -> dict:
        body = {"action": action, "window_s": self.c.twin_window_s, "repetitions": self.c.twin_repetitions}
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

    def judge(self, db, proposal: dict, cluster: dict, action, run: dict) -> gate.Verdict:
        def samples(arm: str) -> dict:
            return {tuple(k.split("/")): [tuple(s) for s in v] for k, v in run["arms"][arm]["samples"].items()}

        slos = {(r["db_role"], r["query_class"]): (r["percentile"], float(r["threshold_ms"])) for r in db.execute(
            "SELECT t.db_role, s.query_class, s.percentile, s.threshold_ms FROM cp.slos s"
            " JOIN cp.tenants t ON t.id = s.tenant_id WHERE t.cluster_id = %s", (cluster["id"],))}
        control, treatment = run["arms"]["control"], run["arms"]["treatment"]
        policy = gate.GatePolicy(warmup_s=min(15.0, run["window_s"] * 0.15))
        return gate.decide(
            samples("control"), samples("treatment"), actions.target_tenant(action), policy,
            mode=proposal["gate_mode"], cheap_to_undo=treatment.get("cheap_to_undo", True),
            wal_ratio=treatment["wal_bytes"] / control["wal_bytes"] if control["wal_bytes"] else None,
            storage_delta_bytes=treatment.get("storage_delta_bytes", 0), slo_ms=slos)

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
                actions.run(plan.apply, prod)
        except (psycopg.Error, actions.ActionRefused) as exc:
            return self.set_state(db, proposal, "FAILED", f"T3: could not apply: {exc}")
        if isinstance(action, (actions.RoleSetting, actions.ConcurrencyCap)):
            self.reconnect_pooler(cluster)
        applied_at = datetime.now(timezone.utc)
        db.execute(
            "INSERT INTO cp.canaries (proposal_id, org_id, applied, inverse, contract, baseline) VALUES (%s, %s, %s, %s, %s, %s)",
            (proposal["id"], proposal["org_id"], Jsonb(plan.apply), Jsonb(plan.inverse), Jsonb(contract), Jsonb(baseline)))
        log.info("proposal %s applied to production; canary started", proposal["id"])

        history: list[dict] = []
        seen: set[datetime] = set()
        deadline = time.monotonic() + self.c.canary_window_s * (self.c.canary_windows + 3)
        reason = None
        while len(history) < self.c.canary_windows:
            time.sleep(min(5.0, self.c.canary_window_s / 4))
            fresh = [w for w in self.latency_windows(db, cluster["id"], applied_at) if w["end"] not in seen
                     # A window that began before the change mixes old and new behaviour; skip it.
                     and (w["end"] - applied_at).total_seconds() >= self.c.canary_window_s * 0.9]
            for w in fresh:
                seen.add(w["end"])
                history.append({"end": w["end"].isoformat(), "telemetry": True,
                                "ratios": {k: round(v[0] / baseline[k], 3) for k, v in w["keys"].items() if k in baseline},
                                "counts": {k: v[1] for k, v in w["keys"].items()},
                                "breaches": window_breaches(baseline, w["keys"], contract, self.c.canary_min_txns)})
            if time.monotonic() > deadline and len(history) < self.c.canary_windows:
                history.append({"end": datetime.now(timezone.utc).isoformat(), "telemetry": False, "ratios": {},
                                "counts": {}, "breaches": []})
                history.append(dict(history[-1]))
            db.execute("UPDATE cp.canaries SET observations = %s WHERE proposal_id = %s", (Jsonb(history), proposal["id"]))
            reason = None if observe_only else rollback_reason(history)
            if reason:
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

        summary = "observed without enforcement" if observe_only else "contract held for every tenant"
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
