"""Checks and restores the demo's expected state through the public API, as the demo administrator.

    python scripts/demo_admin.py status                 is the demo ready to show? (read-only)
    python scripts/demo_admin.py reset                  undo what a previous demonstration left behind
    python scripts/demo_admin.py reset --clear-queue    ... and reject proposals that are only waiting for a decision
    python scripts/demo_admin.py orgs                   list the organizations this account belongs to (GET /orgs)
    python scripts/demo_admin.py org-create "Some Name" create one through the API, as the console does (POST /orgs)

It uses the same endpoints as the console, so everything it does is audited. It never touches the data plane
directly and never deletes anything: a proposal is rolled back or rejected, which keeps its record.
Credentials are read from .env (DEMO_ADMIN_EMAIL, DEMO_ADMIN_PASSWORD) and never printed.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8000/api/v1"
IN_FLIGHT = ("PROPOSED", "VERIFYING", "APPROVED", "CANARY", "ROLLBACK_REQUESTED")
WAITING = ("AWAITING_APPROVAL", "INCONCLUSIVE")
DEMO_ROLE, DEMO_CLASS, DEMO_PERCENTILE, REGISTERED_MS = "t_analytic", "OLAP", 95, 2000


def read_env() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        sys.exit("There is no .env. Run scripts/make_env.sh first.")
    return {k: v.strip() for k, _, v in (l.partition("=") for l in path.read_text(encoding="utf-8").splitlines()
                                         if "=" in l and not l.lstrip().startswith("#"))}


class Api:
    def __init__(self, env: dict[str, str]):
        try:
            body = self.call("POST", "/auth/login", {"email": env.get("DEMO_ADMIN_EMAIL", "admin@demo.dbpilot.dev"),
                                                      "password": env["DEMO_ADMIN_PASSWORD"]}, auth=False)
        except urllib.error.HTTPError as exc:
            sys.exit(f"Could not sign in as the demo administrator (HTTP {exc.code}). Has demo-seed run? "
                     "Try: docker compose run --rm demo-seed")
        except OSError as exc:
            sys.exit(f"The API is not reachable at {API} ({exc}). Is the stack up?")
        self.token = body["access_token"]
        self.org = body["org"]

    token = ""

    def call(self, method: str, path: str, body=None, auth: bool = True):
        request = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if auth else {})})
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else None


def observed_cluster(api: Api) -> dict:
    found = next((c for c in api.call("GET", "/clusters") if c.get("primary_host") and c["status"] != "RETIRED"), None)
    if not found:
        sys.exit(f"{api.org['name']} has no observed cluster. Register the demo data plane under Settings, or run demo-seed.")
    return found


def proposals(api: Api, cluster: dict) -> list[dict]:
    return api.call("GET", f"/proposals?cluster_id={cluster['id']}&limit=500")


def status(api: Api) -> int:
    cluster = observed_cluster(api)
    problems, notes = [], []
    tenants = api.call("GET", f"/tenants?cluster_id={cluster['id']}")
    slo = api.call("GET", f"/clusters/{cluster['id']}/slo-status?minutes=15")
    instance = api.call("GET", f"/clusters/{cluster['id']}/instance?minutes=15")
    ps = proposals(api, cluster)
    agent = api.call("GET", "/agent/status")
    print(f"organization   {api.org['name']} ({api.org['role'].lower()})")
    print(f"cluster        {cluster['name']}, {len(tenants)} tenants")
    if not instance:
        problems.append("the collector has written nothing in 15 minutes (is the stack up?)")
    else:
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(instance[-1]["window_end"].replace("Z", "+00:00"))).total_seconds()
        print(f"collector      last snapshot {age:.0f} s ago")
        if age > 180:
            problems.append(f"the collector's last snapshot is {age:.0f} s old")
    traffic = [s for s in slo if s["windows"] > 0]
    print(f"telemetry      {len(traffic)} of {len(slo)} objectives have observations in the last 15 minutes")
    if not traffic:
        problems.append("no tenant traffic: start it with  docker compose --profile demo up -d demo-load")
    target = next((s for s in slo if s["tenant_role"] == DEMO_ROLE and s["query_class"] == DEMO_CLASS), None)
    if target:
        print(f"objective      {DEMO_ROLE} {DEMO_CLASS} p{target['percentile']} under {target['threshold_ms']:.0f} ms (registered: {REGISTERED_MS}); observed {target['observed_ms']:.1f} ms")
        if target["threshold_ms"] != REGISTERED_MS:
            notes.append(f"the {DEMO_ROLE} objective was tightened for a demonstration; `reset` restores it")
    counts: dict[str, int] = {}
    for p in ps:
        counts[p["state"]] = counts.get(p["state"], 0) + 1
    print("proposals      " + (", ".join(f"{n} {s.lower().replace('_', ' ')}" for s, n in sorted(counts.items())) or "none"))
    for state in IN_FLIGHT + ("APPLIED",):
        if counts.get(state):
            problems.append(f"{counts[state]} proposal(s) are {state}: wait for them, or run `reset`")
    waiting = sum(counts.get(s, 0) for s in WAITING)
    if waiting:
        notes.append(f"{waiting} proposals wait for a decision (old demo runs); `reset --clear-queue` rejects them, keeping their records")
    print(f"LLM proposer   " + (f"{agent['provider']}, model {agent['model']}" if agent["configured"] else "not configured (the rule-based proposer is always available)"))
    for n in notes:
        print(f"note           {n}")
    for p in problems:
        print(f"PROBLEM        {p}")
    print("READY" if not problems else "NOT READY")
    return 0 if not problems else 1


def wait_until(api: Api, cluster: dict, states: tuple[str, ...], minutes: float) -> list[dict]:
    deadline = time.time() + minutes * 60
    while True:
        left = [p for p in proposals(api, cluster) if p["state"] in states]
        if not left or time.time() > deadline:
            return left
        print(f"  waiting for {len(left)} proposal(s) in {', '.join(sorted({p['state'] for p in left}))} ...", flush=True)
        time.sleep(20)


def reset(api: Api, clear_queue: bool, wait_minutes: float) -> int:
    cluster = observed_cluster(api)
    tenant = next((t for t in api.call("GET", f"/tenants?cluster_id={cluster['id']}") if t["db_role"] == DEMO_ROLE), None)
    if tenant:
        api.call("PUT", f"/tenants/{tenant['id']}/slos", {"query_class": DEMO_CLASS, "percentile": DEMO_PERCENTILE, "threshold_ms": REGISTERED_MS})
        print(f"objective of {DEMO_ROLE} restored to p{DEMO_PERCENTILE} under {REGISTERED_MS} ms")
    print("waiting for changes still in flight to finish (they are never cancelled half-way) ...")
    stuck = wait_until(api, cluster, IN_FLIGHT, wait_minutes)
    for p in [p for p in proposals(api, cluster) if p["state"] == "APPLIED"]:
        print(f"rolling back {p['action'].get('type')} ({p['id'][:8]}) ...")
        api.call("POST", f"/proposals/{p['id']}/rollback", {"reason": "demo reset"})
    stuck += wait_until(api, cluster, IN_FLIGHT + ("APPLIED",), wait_minutes)
    rejected = 0
    if clear_queue:
        for p in proposals(api, cluster):
            if p["state"] in WAITING:
                api.call("POST", f"/proposals/{p['id']}/reject", {"reason": "demo reset: stale proposal from an earlier run"})
                rejected += 1
        print(f"rejected {rejected} proposal(s) that were only waiting for a decision (records kept)")
    remaining = [p for p in proposals(api, cluster) if p["state"] in IN_FLIGHT + ("APPLIED",)]
    if remaining:
        print(f"NOT RESTORED: {len(remaining)} proposal(s) still {', '.join(sorted({p['state'] for p in remaining}))}")
        return 1
    print("demo state restored")
    return 0


def orgs(api: Api) -> int:
    rows = api.call("GET", "/orgs")
    print(f"{'name':32s} {'identifier':28s} {'role':9s} {'members':>7s} {'clusters':>8s} {'tenants':>7s}")
    for o in rows:
        print(f"{o['name']:32s} {o['slug']:28s} {o['role'].lower():9s} {o['members']:7d} {o['clusters']:8d} {o['tenants']:7d}" + ("  <- current" if o["current"] else ""))
    return 0


def org_create(api: Api, name: str) -> int:
    try:
        created = api.call("POST", "/orgs", {"name": name})
    except urllib.error.HTTPError as exc:
        print(f"refused (HTTP {exc.code}): {json.loads(exc.read() or b'{}').get('detail', 'no detail')}")
        return 1
    print(f"created {created['name']!r} (identifier {created['slug']}); you are its {created['role'].lower()}. Refresh the console's Organizations page.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "reset", "orgs", "org-create"])
    parser.add_argument("name", nargs="?", help="organization name, for org-create")
    parser.add_argument("--clear-queue", action="store_true")
    parser.add_argument("--wait-minutes", type=float, default=30)
    args = parser.parse_args()
    api = Api(read_env())
    if args.command == "orgs":
        return orgs(api)
    if args.command == "org-create":
        if not args.name:
            sys.exit('org-create needs a name: python scripts/demo_admin.py org-create "Some Name"')
        return org_create(api, args.name)
    return status(api) if args.command == "status" else reset(api, args.clear_queue, args.wait_minutes)


if __name__ == "__main__":
    sys.exit(main())
