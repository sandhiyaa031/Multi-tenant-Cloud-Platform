"""Registers the development data plane with the control plane through the
public API: one organization, one cluster, the four seeded tenants and an SLO
for each. Safe to re-run; anything that already exists is left alone."""
import os
import sys

import httpx

API = os.environ.get("API_URL", "http://api:8000") + "/api/v1"
EMAIL = os.environ["DEMO_ADMIN_EMAIL"]
PASSWORD = os.environ["DEMO_ADMIN_PASSWORD"]

TENANTS = [
    # name, role, warehouse range, profile, (query class, percentile, threshold ms)
    ("steady", "t_steady", 1, 2, "STEADY_OLTP", ("OLTP", 99, 100)),
    ("bursty", "t_bursty", 3, 4, "BURSTY_OLTP", ("OLTP", 99, 150)),
    ("analytic", "t_analytic", 5, 8, "ANALYTICAL", ("OLAP", 95, 2000)),
    ("mixed", "t_mixed", 9, 12, "MIXED", ("OLTP", 99, 150)),
]


def main() -> int:
    with httpx.Client(timeout=30) as http:
        r = http.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if r.status_code == 401:
            r = http.post(
                f"{API}/auth/signup",
                json={"org_name": "Demo Org", "email": EMAIL, "password": PASSWORD, "full_name": "Demo Admin"},
            )
        r.raise_for_status()
        http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        clusters = {c["name"]: c for c in http.get(f"{API}/clusters").raise_for_status().json()}
        if "demo" not in clusters:
            r = http.post(
                f"{API}/clusters",
                json={"name": "demo", "pooler_host": "pgbouncer", "pooler_port": 6432, "database_name": "app",
                      "primary_host": "dp-primary", "primary_port": 5432},
            )
            r.raise_for_status()
            clusters["demo"] = r.json()
        cluster_id = clusters["demo"]["id"]

        existing = {t["name"]: t for t in http.get(f"{API}/tenants", params={"cluster_id": cluster_id}).json()}
        for name, role, lo, hi, profile, (query_class, percentile, threshold) in TENANTS:
            if name not in existing:
                r = http.post(
                    f"{API}/tenants",
                    json={"cluster_id": cluster_id, "name": name, "db_role": role, "warehouse_lo": lo,
                          "warehouse_hi": hi, "profile": profile},
                )
                r.raise_for_status()
                existing[name] = r.json()
            http.put(
                f"{API}/tenants/{existing[name]['id']}/slos",
                json={"query_class": query_class, "percentile": percentile, "threshold_ms": threshold},
            ).raise_for_status()

    print(f"demo organization ready: cluster {cluster_id}, {len(TENANTS)} tenants, login {EMAIL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
