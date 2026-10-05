"""Evaluation harness: one trial = one scenario under one verification configuration.

A trial runs the scenario's workload, submits the scenario's action through the
public API with the configuration's verification settings, follows the proposal
to its final state, undoes whatever was applied, and writes one JSON line.

Ground truth comes from the load generator's own client-side measurements, not
from DBPilot's telemetry. For each tenant and class, p95 latency while the
change was live in production is compared with p95 just before it and, once it
has been undone, just after: live / mean(before, after). Using both sides
cancels slow drift in the machine, which a plain before/after comparison
would count as an effect of the change. That ratio is what "did this action
harm a tenant" means here.

    python -m evaluation --scenario S1_missing_index --config C4_twin_per_tenant
    python -m evaluation --matrix --reps 3        # every scenario under every configuration; resumable
    python -m evaluation --list
"""
import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from workload.driver import Recorder, run

SPEC = json.loads((Path(__file__).parent / "scenarios.json").read_text(encoding="utf-8"))
TERMINAL = {"APPLIED", "REJECTED", "INCONCLUSIVE", "ROLLED_BACK", "FAILED", "ADVISORY", "AWAITING_APPROVAL"}
HARM_RATIO = 1.10
INTERVAL_S = 10.0


RATE_SCALE = float(os.environ.get("EVAL_RATE_SCALE", "1.0"))
POOL_SIZE = int(os.environ.get("EVAL_POOL_SIZE", "16"))


def profile_for(scenario: dict, rate_scale: float = RATE_SCALE) -> dict:
    """The scenario's workload. `rate_scale` shrinks every stream's request rate for a
    smaller machine; the mix of tenants, classes and bursts is unchanged."""
    tenants = []
    for tenant in SPEC["base_tenants"]:
        streams = scenario["overrides"].get(tenant["role"], tenant["streams"])
        tenants.append({"role": tenant["role"], "streams": [{**s, "rate": s["rate"] * rate_scale} for s in streams]})
    return {"tenants": tenants}


class Api:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=os.environ.get("API_URL", "http://api:8000") + "/api/v1", timeout=120)

    async def login(self) -> str:
        r = await self.http.post("/auth/login", json={"email": os.environ["DEMO_ADMIN_EMAIL"],
                                                       "password": os.environ["DEMO_ADMIN_PASSWORD"]})
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        clusters = (await self.http.get("/clusters")).raise_for_status().json()
        return next(c["id"] for c in clusters if c["primary_host"])

    async def get(self, path: str):
        return (await self.http.get(path)).raise_for_status().json()

    async def post(self, path: str, body: dict):
        return (await self.http.post(path, json=body)).raise_for_status().json()


def p95_by_key(intervals: list[dict], start: float, end: float) -> dict[str, float]:
    """Median of per-interval p95 between two wall-clock times, per "tenant/CLASS"."""
    grouped: dict[str, list[float]] = {}
    for row in intervals:
        if start <= row["wall"] < end and row["count"] >= 5:
            grouped.setdefault(f"{row['tenant']}/{row['class']}", []).append(row["p95_ms"])
    return {k: statistics.median(v) for k, v in grouped.items() if len(v) >= 2}


async def ensure_clean(api: "Api", cluster: str) -> None:
    """A trial must start from the unmodified cluster: wait for anything in flight, undo anything applied."""
    for _ in range(240):
        busy = [p for p in await api.get(f"/proposals?cluster_id={cluster}&limit=200")
                if p["state"] in ("PROPOSED", "VERIFYING", "APPROVED", "CANARY", "ROLLBACK_REQUESTED", "APPLIED")]
        if not busy:
            return
        for p in busy:
            if p["state"] == "APPLIED":
                await api.post(f"/proposals/{p['id']}/rollback", {"reason": "evaluation: restoring a clean cluster"})
        await asyncio.sleep(5)
    raise SystemExit(f"cluster is not clean: {len(busy)} proposal(s) still in flight or applied")


async def trial(scenario_name: str, config_name: str, warm_s: float, post_s: float, max_s: float) -> dict:
    scenario, config = SPEC["scenarios"][scenario_name], SPEC["configurations"][config_name]
    api = Api()
    cluster = await api.login()
    await ensure_clean(api, cluster)

    host, password = os.environ.get("DP_POOLER_HOST", "pgbouncer"), os.environ["DP_TENANT_PASSWORD"]
    recorder = Recorder(out_path=None, interval_s=INTERVAL_S)
    load = asyncio.create_task(run(
        profile_for(scenario), lambda role: f"host={host} port=6432 dbname=app user={role} password={password}",
        max_s, None, interval_s=INTERVAL_S, pool_size=POOL_SIZE, recorder=recorder))

    result: dict = {"scenario": scenario_name, "config": config_name, "action": scenario["action"],
                    "started": datetime.now(timezone.utc).isoformat(), "harm_ratio": HARM_RATIO,
                    "rate_scale": RATE_SCALE, "pool_size": POOL_SIZE}
    try:
        # Long enough for the twin source's delay window to be filled with this workload.
        await asyncio.sleep(warm_s)
        submitted = time.time()
        proposal = await api.post(f"/clusters/{cluster}/proposals", {
            "action": scenario["action"], "rationale": f"evaluation: {scenario_name} under {config_name}",
            "verification": config["verification"], "gate_mode": config["gate_mode"], "auto_approve": True})
        pid = proposal["id"]

        applied_at = None
        while time.time() - submitted < max_s - warm_s - post_s - 20:
            await asyncio.sleep(5)
            detail = await api.get(f"/proposals/{pid}")
            if detail["canary"] and applied_at is None:
                applied_at = time.time()
            if detail["state"] in TERMINAL:
                break
        decided = time.time()
        live_until = decided
        if detail["state"] == "APPLIED":
            # Keep the change live a little longer, to measure its effect outside the canary windows too.
            await asyncio.sleep(post_s)
            live_until = time.time()

        # Undo whatever is still applied, then keep the load running to measure the "after" side.
        cleanup = None
        if detail["state"] == "APPLIED":
            await api.post(f"/proposals/{pid}/rollback", {"reason": "evaluation cleanup"})
            for _ in range(60):
                await asyncio.sleep(3)
                cleanup = (await api.get(f"/proposals/{pid}"))["state"]
                if cleanup in ("ROLLED_BACK", "FAILED"):
                    break
        undone_at = time.time()
        undoable = bool(detail["canary"] and detail["canary"]["inverse"])
        if applied_at:
            await asyncio.sleep(post_s + INTERVAL_S)

        before = p95_by_key(recorder.intervals, submitted - warm_s * 0.6, submitted)
        during = p95_by_key(recorder.intervals, applied_at + INTERVAL_S, live_until) if applied_at else {}
        after = p95_by_key(recorder.intervals, undone_at + INTERVAL_S, time.time()) if applied_at and undoable else {}
        simple = {k: round(during[k] / before[k], 3) for k in during if k in before and before[k] > 0}
        ratios = {k: round(during[k] / ((before[k] + after[k]) / 2), 3) for k in during
                  if k in before and k in after and before[k] > 0}
        judged = ratios or simple
        target = scenario["action"].get("tenant_role")
        harmed = sorted(k for k, r in judged.items() if r > HARM_RATIO and (target is None or not k.startswith(target + "/")))
        twin = detail["twin_runs"][-1] if detail["twin_runs"] else None
        shadow = twin and twin["verdict"].get("shadow")
        ratios = judged
        result.update(
            twin_looks=twin and twin["verdict"].get("looks"), twin_repetitions=twin and twin["repetitions"],
            twin_shadow_mode=shadow and shadow["mode"], twin_shadow_decision=shadow and shadow["decision"],
            twin_shadow_reasons=shadow and shadow["reasons"],
            calibration=twin and twin["verdict"].get("calibration"),
            exposure_s=round(live_until - applied_at, 1) if applied_at else 0.0,
            canary_stages=detail["canary"] and max((o.get("stage", 1) for o in detail["canary"]["observations"]), default=1),
            client_p95_after_ms={k: round(v, 2) for k, v in after.items()},
            client_ratios_before_after_only=simple, cleanup=cleanup,
            ground_truth="live / mean(before, after)" if after else "live / before",
            proposal_id=pid, final_state=detail["state"], state_reason=detail["state_reason"],
            steps=[{"tier": s["tier"], "decision": s["decision"], "summary": s["summary"], "seconds": round(s["seconds"], 1)}
                   for s in detail["steps"]],
            seconds_to_decision=round(decided - submitted, 1),
            reached_production=applied_at is not None,
            twin_decision=twin and twin["verdict"]["decision"],
            twin_ratios=twin and {k: e["ratio"] and round(e["ratio"], 3) for k, e in twin["verdict"]["effects"].items()},
            twin_intervals=twin and {k: [e["lo"] and round(e["lo"], 3), e["hi"] and round(e["hi"], 3)]
                                     for k, e in twin["verdict"]["effects"].items()},
            twin_replay_errors=twin and twin["replay_errors"], twin_transactions=twin and twin["transactions"],
            canary_outcome=detail["canary"] and detail["canary"]["outcome"],
            dbpilot_production_ratios=detail["canary"] and detail["canary"]["result"],
            client_p95_before_ms={k: round(v, 2) for k, v in before.items()},
            client_p95_during_ms={k: round(v, 2) for k, v in during.items()},
            client_ratios=ratios, tenants_harmed=harmed,
        )
    finally:
        load.cancel()
        await asyncio.gather(load, return_exceptions=True)
        await api.http.aclose()
    return result


def matrix(args) -> None:
    """Repetition by repetition, so that stopping early still leaves complete repetitions.
    Trials already in the output file are skipped: the run can be stopped and resumed."""
    scenarios = args.scenarios.split(",") if args.scenarios else list(SPEC["scenarios"])
    configs = args.configs.split(",") if args.configs else list(SPEC["configurations"])
    done = set()
    if Path(args.out).exists():
        for line in Path(args.out).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if "final_state" in row:
                done.add((row["scenario"], row["config"], row.get("rep")))
    for rep in range(args.reps):
        for scenario in scenarios:
            for config in configs:
                if (scenario, config, rep) in done:
                    continue
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}] rep {rep} {scenario} {config}", flush=True)
                try:
                    outcome = asyncio.run(trial(scenario, config, args.warm, args.post, args.max))
                except Exception as exc:  # recorded, so a failed trial is visible in the data
                    outcome = {"scenario": scenario, "config": config, "error": f"{type(exc).__name__}: {exc}"}
                outcome["rep"] = rep
                with open(args.out, "a", encoding="utf-8") as f:
                    f.write(json.dumps(outcome) + "\n")
                print(f"    -> {outcome.get('final_state') or outcome.get('error')}"
                      f" harmed={outcome.get('tenants_harmed')}", flush=True)
                time.sleep(20)


def main() -> None:
    parser = argparse.ArgumentParser(prog="evaluation")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--scenario")
    parser.add_argument("--config")
    parser.add_argument("--warm", type=float, default=float(os.environ.get("EVAL_WARM_S", "150")))
    parser.add_argument("--post", type=float, default=60)
    parser.add_argument("--max", type=float, default=2700, help="upper bound on one trial, seconds")
    parser.add_argument("--out", default="/results/evaluation.jsonl")
    parser.add_argument("--matrix", action="store_true", help="run every scenario under every configuration")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--scenarios", help="comma-separated subset for --matrix")
    parser.add_argument("--configs", help="comma-separated subset for --matrix")
    args = parser.parse_args()
    if args.list:
        for name, s in SPEC["scenarios"].items():
            print(f"{name:<28} {s['description']}")
        print("configurations:", ", ".join(SPEC["configurations"]))
        return
    # A trial takes many minutes; find out now, not afterwards, that its result cannot be written.
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    open(args.out, "a", encoding="utf-8").close()
    if args.matrix:
        return matrix(args)
    outcome = asyncio.run(trial(args.scenario, args.config, args.warm, args.post, args.max))
    with open(args.out, "a", encoding="utf-8") as f:
        f.write(json.dumps(outcome) + "\n")
    summary = {k: outcome.get(k) for k in ("scenario", "config", "final_state", "seconds_to_decision", "twin_decision",
                                           "canary_outcome", "client_ratios", "tenants_harmed", "twin_ratios", "cleanup")}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
