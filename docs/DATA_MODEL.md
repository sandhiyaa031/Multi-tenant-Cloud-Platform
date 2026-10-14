# The data model: organizations, tenants, schema, roles, row-level security and transactions

Read this to answer "where is it stored, who can see it, and what stops them seeing more". It describes the code in
`controlplane/migrations/` (the control plane's own database) and `dataplane/` (the PostgreSQL that the tenants use).
Every claim names the file that makes it true.

## 1. Two databases, and two different meanings of "tenant"

DBPilot touches two PostgreSQL databases that have nothing to do with each other except that DBPilot reads one of them.

| | Control-plane database | Data-plane database |
|---|---|---|
| What it is | DBPilot's own memory: accounts, organizations, telemetry, proposals, audit | The PostgreSQL that DBPilot looks after: the thing being tuned |
| Who writes to it | The API, the collector and the engine, through three separate login roles | The tenants' own workload, and DBPilot's executor when it applies an approved change |
| Where | `control-db` container, database `dbpilot_control`, schema `cp` | `dp-primary` container, database `app`, schema `ch` |
| Defined by | `controlplane/migrations/001..008_*.sql` | `dataplane/postgres/initdb/*.sql` |

Two words that sound alike and are not:

| Word | Where it lives | What it means |
|---|---|---|
| **Organization** | `cp.organizations` (control plane) | A customer **of DBPilot**: a company or team that signs in. It owns members, clusters and an audit trail |
| **Tenant** | `cp.tenants` (a registration) and `ch.tenant_map` plus a database role (the real thing) | A customer **of the database being tuned**: one PostgreSQL login role that owns a range of warehouse ids, whose queries DBPilot measures and protects |

How they connect, in the direction of ownership:

```
organization  1 ──< membership >── n  user          (role: VIEWER < OPERATOR < ADMIN)
organization  1 ──< cluster                         (where one PostgreSQL deployment is: pooler host, database, primary host)
cluster       1 ──< tenant                          (db_role + warehouse range + workload profile)
tenant        1 ──< slo                             (objective per class: "p95 of OLAP under 2 s")
cluster       1 ──< proposal ──< verification_step, twin_run, canary
```

**Creating an organization creates none of the lower rows.** `POST /api/v1/orgs` runs `cp.create_organization()`, which
inserts an organization, its first ADMIN membership and an audit row, in one transaction, and nothing else. A cluster is
registered afterwards (`POST /clusters`), and a tenant after that (`POST /tenants`); both record where an **existing**
PostgreSQL role and warehouse range are. DBPilot never runs `CREATE ROLE` or `CREATE DATABASE` on a data plane. The
tenants of the demo are created by `dataplane/seed/seed.sql` (`ch.provision_tenant`) when the stack is first started,
not by anything in the console. This is also why, on this deployment, the only data plane an organization can register is
the demo one: the collector and the engine reach it with the credentials the deployment was started with.

## 2. The control-plane schema (`cp`)

| Table | Migration | Holds | Notable rules |
|---|---|---|---|
| `organizations` | 001, 008 | `name`, `slug` (unique, `[a-z0-9-]`), `archived_at` | slug is the identifier and never changes; no row is ever deleted (section 6) |
| `users` | 001 | `email` (unique, case-insensitive), `password_hash` (Argon2id), `is_active`, generated `has_password` | no hash means "invited, cannot sign in yet" |
| `memberships` | 001 | `(org_id, user_id)` primary key, `role` | trigger `guard_last_admin` keeps at least one ADMIN per organization |
| `password_reset_tokens` | 001, 004 | SHA-256 hash of a single-use token, purpose `reset` or `invite`, expiry | invisible to the API role: no policy at all |
| `clusters` | 001 | `pooler_host`, `database_name`, `primary_host` (what the collector reads), `status` (`PENDING`, `ACTIVE`, `DEGRADED`, `RETIRED`) | `UNIQUE (org_id, name)` |
| `tenants` | 001 | `cluster_id`, `db_role`, `warehouse_lo/hi`, `profile` | composite FK `(cluster_id, org_id)` so a tenant cannot point at another organization's cluster; `EXCLUDE USING gist` so two tenants of one cluster cannot own overlapping warehouse ranges |
| `slos` | 001 | per tenant: class (`OLTP`/`OLAP`), percentile (50/95/99), threshold in ms | what "meeting its objective" means |
| `audit_log` | 002 | who did what to which entity, as JSON | append-only (section 5) |
| `query_fingerprints`, `query_stats` | 005 | each statement shape, and per-window counters per tenant | `query_stats` is partitioned by day |
| `instance_stats`, `latency_stats` | 005, 006 | cluster counters and p50/p95/p99 per tenant and class per window | written by the collector only |
| `proposals` | 007 | a typed action (`jsonb`), `source` (`manual`/`rule`/`agent`), `verification`, `gate_mode`, `state` | the state machine (section 4); at most one in `CANARY` per cluster |
| `proposal_transitions` | 007 | the legal `(from_state, to_state)` pairs | data, checked by a trigger |
| `verification_steps` | 007 | one row per tier (T0..T3): decision, summary, detail | |
| `twin_runs` | 007 | the digital twin's measurements and the gate's verdict as `jsonb` | |
| `canaries` | 007 | applied statements, their **inverse**, the contract, the baseline, each window's observation, the result | the inverse is stored before the change is applied |
| `outcome_ledger` (view) | 007 | proposal joined with twin prediction and production result | `security_invoker`, so it obeys the caller's row-level security |

## 3. Roles and what each may do (control plane)

`controlplane/migrate.py` creates three login roles, all `NOSUPERUSER NOBYPASSRLS`. The migrations are run by the owner
role, which the running services never use.

| Login role | Used by | May |
|---|---|---|
| `dbpilot_api` | the API | read org-owned tables through row-level security; write clusters, tenants and SLOs (policy-limited by member role); `UPDATE (name)` on organizations; **no** INSERT on users, organizations or the audit log; no access to password hashes or reset tokens |
| `dbpilot_collector` | the collector | read clusters and tenants; write telemetry tables only |
| `dbpilot_engine` | the engine | read and advance proposals, write verification steps, twin runs and canaries, **across all organizations**: its policies are `USING (true)`, because one engine serves every organization's queue. It is a trusted service, not a tenant of the control plane, and it never accepts input from a browser |

Anything that must cross those limits is a `SECURITY DEFINER` function that does one thing and is granted to
`dbpilot_api` by name: `cp.signup`, `cp.auth_get_user`, `cp.auth_memberships`, `cp.create_reset_token`,
`cp.consume_token`, `cp.invite_member` (004), and, from migration 008, `cp.create_organization`,
`cp.archive_organization` and `cp.my_organizations`. `REVOKE ALL ... FROM PUBLIC` removes everyone else's access to them.

## 4. Row-level security: how a request is confined to one organization

Every request that touches org-owned data runs inside one transaction that starts with
(`controlplane/app/db.py: org_tx`):

```sql
SELECT set_config('app.user_id', :user, true), set_config('app.org_id', :org, true);   -- true = this transaction only
```

The two values vanish at COMMIT or ROLLBACK, so a pooled connection can never carry one request's identity into the next.
Every table that belongs to an organization has `ENABLE ROW LEVEL SECURITY` and policies of one shape
(`controlplane/migrations/003_security.sql`):

```sql
USING (org_id = cp.current_org() AND cp.has_role('VIEWER'))      -- reading
WITH CHECK (org_id = cp.current_org() AND cp.has_role('OPERATOR'))  -- writing (the minimum role differs by table)
```

`cp.has_role(min)` asks whether the current user holds at least `min` in the current organization, **and the user is
active, and the organization is not archived**. The role is read from `cp.memberships` on every request, not from the
token, so demoting or removing someone takes effect on their next request.

Consequences you can state with confidence, because tests check each one (`controlplane/tests/test_authorization.py`,
`test_organizations.py`):

- A request with no organization context sees zero rows of every org-owned table.
- A VIEWER cannot write even with hand-written SQL as the API role: the policy refuses it, whatever the endpoint does.
- Organization A's token cannot read B's clusters, tenants, proposals or audit trail. B's ids simply return "not found".
- Authorization is enforced **twice**: the endpoint checks the role (`controlplane/app/deps.py: require_role`), and the
  database checks it again.

## 5. Transactions and triggers: what is atomic, and what cannot be tampered with

| What | Mechanism | File |
|---|---|---|
| Sign-up: organization, first user, ADMIN membership and audit row commit together or not at all | one function, one transaction | `004_auth_functions.sql: cp.signup` |
| Creating another organization: organization, ADMIN membership, audit row | one function, one transaction | `008_organizations.sql: cp.create_organization` |
| An organization always keeps an ADMIN, even if two admins demote each other at the same instant | trigger takes `FOR UPDATE` on the organization row first, so the two checks run one after the other | `002_audit.sql: guard_last_admin` |
| A one-time token cannot be used twice | the function consumes it under a row lock | `004: cp.consume_token` |
| Audit rows exist for every change to clusters, tenants, SLOs, memberships and proposals | `AFTER INSERT/UPDATE/DELETE` triggers call `cp.audit_row()`, a `SECURITY DEFINER` function; the API role has no INSERT on `audit_log`, so it can cause audit rows but never forge them | `002_audit.sql` |
| The audit log cannot be altered, even by the table owner | `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers that always raise | `002_audit.sql` |
| A proposal cannot skip verification, revive a rejected one, or have its action edited | `proposal_guard` trigger checks the move against `proposal_transitions` and refuses changes to `action`, `cluster_id`, `gate_mode`, `verification`, `auto_approve` | `007_proposals.sql` |
| At most one change in canary per cluster, so any regression can be attributed | partial unique index `WHERE state = 'CANARY'` | `007_proposals.sql` |
| Two engines cannot take the same proposal | `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)` | `engine.py: claim` |

The proposal state machine, as the trigger enforces it:

```
PROPOSED -> VERIFYING -> ADVISORY | REJECTED | INCONCLUSIVE | AWAITING_APPROVAL | APPROVED | FAILED
INCONCLUSIVE      -> APPROVED (an admin overrides) | REJECTED
AWAITING_APPROVAL -> APPROVED | REJECTED
APPROVED -> CANARY -> APPLIED | ROLLED_BACK | FAILED
APPLIED -> ROLLBACK_REQUESTED -> ROLLED_BACK | FAILED
```

## 6. Why an organization is archived, not deleted

`audit_log.org_id` references `organizations` with `ON DELETE RESTRICT`, and the audit log refuses deletes. So an
organization with any history (every one has at least its creation) cannot be deleted without either destroying the audit
trail or removing the guarantee that it cannot be destroyed. Neither is acceptable, so "remove" means **archive**
(`cp.archive_organization`, `DELETE /api/v1/orgs/current`):

- `archived_at` is set. `cp.has_role()` returns false for an archived organization, so **every** policy in the database
  closes at once: its token stops working, it disappears from the switcher and from sign-in, and its slug stays taken.
- It is refused (`DP004`, HTTP 409) while the organization has a cluster that is not `RETIRED`. Retiring a cluster
  (`POST /clusters/{id}/retire`) sets `status = 'RETIRED'` and clears `primary_host`, so the collector and engine stop
  touching it; its tenants, telemetry and proposals stay as history. Retiring is refused while a change to that cluster is
  still being verified or applied, or is applied and not yet rolled back, so a half-finished or live change is never abandoned.
- It is refused (`DP005`) when it is the caller's only active organization: archiving it would lock them out.

## 7. The data plane (`ch`): how tenants are separated inside one database

All tenants share the same tables. Three mechanisms keep them apart and individually observable
(`dataplane/postgres/initdb/02_schema.sql`):

1. **A login role per tenant**, member of the group role `ch_tenant`. PostgreSQL's statistics are keyed by role, so every
   query is attributed to a tenant with no tagging.
2. **Row-level security** on eight tables with one policy each, `tenant_rows`: a role sees and writes only rows whose
   warehouse id lies in its range. The range comes from `ch.my_w_lo()` / `ch.my_w_hi()`, which look the caller up in
   `ch.tenant_map` by `session_user` (not `current_user`, which inside a `SECURITY DEFINER` function would be the function's
   owner). A role with no `tenant_map` row gets NULL bounds and therefore sees nothing.
3. **A partition per tenant** of the six large tables, so an index can be built for one tenant alone and PostgreSQL prunes
   the other tenants' partitions at query start. Tenants hold **no privileges on the partitions themselves**; the only way
   in is through the parent table and its policy.

Tenants connect only through PgBouncer. It authenticates them with `pgbouncer.get_auth()`, a `SECURITY DEFINER` function that
returns a verifier for roles that are members of `ch_tenant` and for nothing else, so the owner and service roles cannot be
reached through the pooler at all.

### The executor: what DBPilot may do to a data plane

`dataplane/postgres/executor.sql` creates `dbpilot_executor`, the role the engine logs in as to apply an approved change:

| Needs | How it is granted |
|---|---|
| `CREATE INDEX`, `DROP INDEX`, `ANALYZE` | membership in `ch_owner`, which owns the tables (PostgreSQL has no narrower privilege for building an index) |
| role-level settings, concurrency caps | `CREATEROLE`, plus `ADMIN` (not membership) on each tenant role |
| instance settings | `GRANT ALTER SYSTEM ON PARAMETER` for eight named parameters only; a test checks the list equals `INSTANCE_SETTINGS` in `actions.py` |

It is not a superuser, and an **event trigger** (`ch.executor_ddl_guard`) refuses every DDL command from it except `CREATE
INDEX` and `DROP INDEX`. What remains, and is stated in the file: as a member of the owning role it could in principle read
or change rows, and with `ADMIN` on a tenant role it could reset that tenant's password. PostgreSQL offers no privilege that
separates these. This is the project's honest limit on "least privilege".

## 8. Telemetry: where the numbers on every chart come from

```
dp-primary --pg_stat_statements, pg_stat_database--> collector --> cp.query_stats, cp.instance_stats   (deltas between snapshots)
dp-primary --JSON statement log (dp-logs volume)---> collector --> cp.latency_stats                    (p50/p95/p99 per tenant, class, window)
                                                  \-> twin (the same log is what it replays)
```

`pg_stat_statements` counters only ever grow, so the collector stores the **difference** between snapshots. It has no
percentiles, so percentiles come from the statement log, grouped into transactions per tenant and class by
`core/dbpilot_core/pglog.py`. The console reads these tables through the API; nothing is synthesised on the way.

## 9. Where to look when asked "show me"

```bash
# the organization you created in the console, as the database sees it (run from the repository root)
docker compose exec control-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select slug, name, archived_at from cp.organizations order by created_at desc limit 5"'
docker compose exec control-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select m.role, u.email from cp.memberships m join cp.users u on u.id = m.user_id join cp.organizations o on o.id = m.org_id where o.slug = '"'"'demo-org'"'"'"'
docker compose exec control-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select id, action, entity_type, created_at from cp.audit_log order by id desc limit 10"'
```

The refused-write demonstration (what row-level security does, as the API's own role):

```bash
docker compose exec control-db sh -c 'psql -U dbpilot_api -d "$POSTGRES_DB" -c "select count(*) from cp.proposals"'   # 0 rows: no organization context
```

(That command needs the API role's password; it is `CONTROL_DB_API_PASSWORD` in `.env`. Without one it demonstrates nothing, so
do not rely on it live; the tests in `controlplane/tests/test_authorization.py` make the same point automatically.)
