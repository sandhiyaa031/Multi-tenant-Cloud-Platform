# DBPilot

Safe, tenant-aware autonomous optimisation for shared PostgreSQL.

An AI agent proposes bounded optimisations; DBPilot measures each one on a real
clone of the database under replayed workload, for every tenant, and applies it
only if the target tenant benefits and no other tenant is harmed.

```
OBSERVE → DIAGNOSE → PLAN → DIGITAL TWIN → VERIFY → CANARY → MONITOR → ROLLBACK → LEARN
```

- [Architecture specification](docs/ARCHITECTURE.md)
- [Learning guide](docs/LEARNING_GUIDE.md)

## Build status

| Milestone | Content | Status |
|---|---|---|
| M0 | Repository reset, Compose scaffold | done |
| M1 | Control-plane schema, auth, RBAC, RLS, audit | done, 39 tests |
| M2a | Data plane: tenant-partitioned schema, per-tenant roles and RLS, replica, PgBouncer, seed data | done, 18 tests |
| M2b | Workload driver, telemetry collector | not started |
| M3 | Scenarios and baselines | not started |
| M4 | Digital twin and fidelity study | not started |
| M5 | Agent, action planner, executor | not started |
| M6 | Verification engine, canary, outcome ledger | not started |
| M7 | Web UI | not started |
| M8 | k3s deployment | not started |
| M9 | Evaluation | not started |

No experimental results exist yet. The previous prototype and its results are
preserved at the git tag `v1-archive` and are not evidence for this system.

## Run

```bash
cp .env.example .env        # then fill in the values
docker compose up -d --build
docker compose run --rm dp-seed                               # ~5 min: four tenants, 12 warehouses
docker compose run --rm --no-deps api python -m pytest -q    # control plane
docker compose run --rm dp-test                               # data plane
```

API: http://localhost:8000/docs

## Layout

```
controlplane/   FastAPI control plane and SQL migrations
dataplane/      managed PostgreSQL: schema, tenancy, replica, PgBouncer, seed
docs/           architecture specification and learning guide
```
