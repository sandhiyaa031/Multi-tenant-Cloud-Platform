# DBPilot: DBMS project review. Complete material for the deck

Everything below was read off the repository (SQL migrations, code, evidence files) and, where stated, checked against the
running stack on 2026-10-14. Nothing here is invented. Where a claim is only documented (not re-run by me), it says so.
Section 8 lists exactly what was verified and what was not.

**How to use this file.** Build the deck in Canva slide by slide. Each slide has: **On the slide** (put this text up, it is
deliberately short), **Visual** (what to draw or paste), **Say** (a plain-language script, use your own words), and
**Source** (the file that proves it, in case someone asks). Slides marked **[CORE]** are the ones to keep for a 1-hour slot.

Files you will paste into Canva are in `docs/review/diagrams/`:

| File | Use |
|---|---|
| `DBPilot_ER_diagram.png` / `.svg` | Slide 9: the single complete ER diagram (9200 x 7359 px, zoomable) |
| `DBPilot_ER_zoom_1_identity_and_resources.png` | Slide 10 |
| `DBPilot_ER_zoom_2_tenant_telemetry.png` | Slide 11 |
| `DBPilot_ER_zoom_3_proposal_lifecycle.png` | Slide 12 |
| `DBPilot_ER_zoom_4_data_plane.png` | Slide 13 |

(The zooms are crops of the same diagram, so they match it exactly. `make_chen.py` regenerates all of them.)

---

## 1. The title

**Recommended:**

> **DBPilot: Tenant-Safe Optimisation for Shared Multi-Tenant PostgreSQL**
> *A cloud database platform that tests every change against every tenant before it touches production*

Why this one: it names the product, says the DBMS (PostgreSQL), says the cloud idea (multi-tenant), and the subtitle states
the one idea that makes it different. It does not say "autonomous" (a person approves changes) and does not say "proves safe"
(the results do not support that, see slide 37).

Alternatives, if you prefer a different tone:

1. **DBPilot: Safe, Tenant-Aware Tuning for Shared PostgreSQL** (the repository's own tagline, shortest)
2. **DBPilot: A Multi-Tenant Cloud Database Platform with Verified, Tenant-Aware Optimisation** (most formal, good for a report cover)
3. **Look Before You Tune: Protecting Every Tenant in a Shared PostgreSQL Cloud** (catchy, but put "DBPilot" in the subtitle)

---

## 2. Deck plan and timing

40 slides, 7 parts, plus 6 backup slides. At about 2.5 minutes a slide that is 100 minutes with the demo; for a 1-hour slot
use only the **[CORE]** slides (31 of them, about 2 minutes each).

| Part | Slides | Minutes (full) | Purpose |
|---|---|---|---|
| 1. Problem and idea | 1-5 | 10 | Why this exists |
| 2. Architecture | 6-8 | 7 | The big picture |
| 3. Database design | 9-18 | 30 | **The DBMS core: ER, schema, constraints, concept map** |
| 4. DBMS concepts in action | 19-24 | 15 | Proof that each concept is really used |
| 5. How DBPilot decides | 25-30 | 12 | The optimisation pipeline, kept simple |
| 6. Evidence | 31-37 | 12 | Verified results and honest limits |
| 7. Demo and close | 38-40 | 10-20 | Live demo, conclusion, questions |

Order rule your professor will like: **design first (ER, schema, constraints), then the concept map, then the proof, then the
application.** That is why the DBMS material comes before the optimisation pipeline.

---

## 3. Slide-by-slide content

### PART 1. THE PROBLEM AND THE IDEA

#### Slide 1. Title **[CORE]**
**On the slide:** the title and subtitle from section 1; your name(s), guide, college, date.
**Visual:** clean cover; a faint database/tenants motif is enough.
**Say:** "This is DBPilot, a multi-tenant PostgreSQL platform. I will show you the database design first, how each DBMS concept is used, and then how the system tunes a shared database safely."

#### Slide 2. Roadmap
**On the slide:** five steps in a row: *Problem -> Architecture -> Database design -> How it decides -> Evidence and demo*.
**Visual:** a five-step arrow strip, "Database design" highlighted.
**Say:** "The middle part, the database design, is the core of this review. The rest explains why the design looks the way it does and shows that it works."

#### Slide 3. The problem **[CORE]**
**On the slide:**
- Many customers (tenants) share one PostgreSQL server.
- A fix for one tenant can slow another.
- Looking only at the overall average hides this.

**Visual:** one server in the middle, four tenant icons around it; one gets a green "faster" badge, a neighbour gets a red "slower?" badge. Caption: "The average improves. A tenant gets hurt."
**Say:** "Cloud databases are shared to save cost. Suppose we add an index to speed up one tenant's reports. An index also makes every write slower, so a neighbour who writes a lot might pay for it. If we only look at the average over everyone, we can approve a change that quietly hurts one tenant. The question this project asks is how to make changes to a shared database without hurting any tenant."
**Note (be honest):** say "can", not "does". In this project's own tests no change was actually found that hurt a neighbour (slide 36).
**Source:** `docs/REPORT.md` section 1.

#### Slide 4. The idea in one picture **[CORE]**
**On the slide:** *Observe -> Propose -> Verify -> Canary -> Roll back if needed*, with one line under each:
- Observe: collect per-tenant statistics.
- Propose: one typed change (an index, a setting).
- Verify: four checks, cheapest first.
- Canary: apply under watch.
- Roll back: automatic if a tenant breaks its limit.

**Visual:** a left-to-right pipeline with the PostgreSQL database in the background; everything is written to the control database.
**Say:** "DBPilot never changes production on a hunch. Something proposes one change. We test it four ways, the last being production itself under close watch, and we undo it automatically if any tenant gets worse. Everything that happens is recorded in tables, which is why this is a database project."
**Source:** `docs/ARCHITECTURE.md` section 1.

#### Slide 5. Scope: what it does and does not do
**On the slide:**
| It does | It does not |
|---|---|
| Change only from a fixed menu of 9 typed actions | Run arbitrary SQL (even from the AI) |
| Require verification before production | Skip a check or approve itself (a person decides) |
| Keep an undo for every change | Create tenants' roles or databases |

**Visual:** two-column table.
**Say:** "The AI agent cannot write SQL. It can only fill in a form: create this index, set that parameter. The form is validated, compiled to SQL by fixed code, and every change has its undo stored first."
**Source:** `core/dbpilot_core/actions.py`; `docs/DATA_MODEL.md` section 1.

---

### PART 2. ARCHITECTURE

#### Slide 6. System architecture **[CORE]**
**On the slide:** three planes and what is in each.
- **Control plane:** web console, API, control database, collector, engine.
- **Data plane:** tenants' workload -> PgBouncer -> primary PostgreSQL (+ replica).
- **Experimentation plane:** digital twin (delayed standby, two clones, replay).

**Visual:** block diagram, three coloured boxes. Arrows: workload -> PgBouncer -> primary; primary -> replica (streaming); primary -> collector -> control DB; engine -> twin; engine -> executor -> primary.
**Say:** "Three planes. The data plane is the database the tenants use. The control plane is DBPilot's brain and memory. The twin is a separate place where we can test a change on a copy, so testing never disturbs production. Today all of it runs on one laptop under Docker Compose; Kubernetes manifests were written for three machines but never deployed."
**Source:** `docs/ARCHITECTURE.md` sections 3 and 14; running containers (api, web, engine, collector, twin, control-db, dp-primary, dp-replica, pgbouncer, demo-load).

#### Slide 7. Technology and why
**On the slide:**
| Piece | Why |
|---|---|
| PostgreSQL 18 | the target DBMS and the control store; RLS, partitioning, triggers, extensions |
| PgBouncer | pooling, per-role connection limits |
| `pg_stat_statements`, HypoPG | per-role statistics; "what-if" hypothetical indexes |
| FastAPI + psycopg 3 | API |
| React + Vite + TypeScript | console |
| Docker Compose | one-command stack |

**Visual:** logo strip with one-line reasons.
**Say:** "We deliberately used PostgreSQL's own features rather than extra tools: statistics views, row-level security, partitioning and triggers do most of the work."
**Source:** `docs/ARCHITECTURE.md` section 13.

#### Slide 8. Two databases, two meanings of "tenant" **[CORE]**
**On the slide:**
| | Control-plane DB | Data-plane DB |
|---|---|---|
| What | DBPilot's memory | the database being tuned |
| Schema | `cp` (17 tables, 1 view) | `ch` (13 tables, 24 partitions) |
| "Tenant" means | a registration row | a PostgreSQL login role that owns warehouses |

And a line: **Organization** = a customer *of DBPilot*. **Tenant** = a customer *of the database being tuned*.
**Visual:** two cylinders joined by a dashed line "same tenant, no cross-database foreign key".
**Say:** "These two words confuse everyone, so I define them now. An organization signs in to DBPilot. A tenant is one of the database's users whose queries we protect. Creating an organization creates no tenants."
**Source:** `docs/DATA_MODEL.md` section 1.

---

### PART 3. DATABASE DESIGN (THE CORE)

#### Slide 9. The complete ER diagram **[CORE]**
**On the slide:** just the title "Entity-relationship diagram: the whole design" and the image.
**Visual:** `DBPilot_ER_diagram.png`, full width. Legend is already inside the image.
**Say:** "This is the whole design in one picture. Green rectangles are entities, blue diamonds are relationships, pink ovals are attributes, underlined ones are primary keys. The top band is the control-plane database, the bottom band is the data-plane database. They are linked at one point: the same tenant is recorded in both. I will now zoom into each region."
**Tips for Canva:** place it at full width, then present it by zooming (or use the four zoom slides). Do not read it all out. Point at: USER-MEMBER OF-ORGANIZATION (many-to-many), CLUSTER-TENANT, PROPOSAL and its three children.
**Honest notes:** the control plane has 17 tables; 16 are drawn as entities and `memberships` is the relationship MEMBER OF (it carries the attributes `role` and `created_at`). Dashed diamonds in the data plane are logical links the database does not declare as foreign keys.

#### Slide 10. ER zoom 1: identity, organizations, resources **[CORE]**
**On the slide:** image zoom 1 and three callouts:
- USER and ORGANIZATION are **many-to-many** through MEMBER OF (role: VIEWER < OPERATOR < ADMIN).
- ORGANIZATION 1 : n CLUSTER 1 : n TENANT.
- AUDIT_LOG records everything; it can never be edited.

**Say:** "A person can belong to many organizations and an organization has many people, so that is a many-to-many relationship, implemented as the memberships table which also stores the role. An organization owns clusters, a cluster hosts tenants. The audit log is a separate append-only record."
**Source:** `controlplane/migrations/001_foundation.sql`, `002_audit.sql`.

#### Slide 11. ER zoom 2: tenants and telemetry **[CORE]**
**On the slide:** image zoom 2 and callouts:
- A tenant has objectives (SLO), per-query statistics, and latency percentiles.
- QUERY_STATS is **partitioned by day**.
- Primary keys here are **composite** (tenant, query, window).

**Say:** "Every minute or so a collector reads the data plane's statistics and stores the difference since the last reading. Percentiles come from the database's statement log because PostgreSQL's statistics view has no percentiles. Old statistics are deleted by dropping a whole day's partition, which is instant."
**Source:** `005_telemetry.sql`, `006_latency.sql`.

#### Slide 12. ER zoom 3: the optimisation lifecycle **[CORE]**
**On the slide:** image zoom 3 and callouts:
- One PROPOSAL (a typed change) has many VERIFICATION_STEPs, TWIN_RUNs and one CANARY.
- PROPOSAL_TRANSITION lists the 19 legal state moves, enforced by a trigger.
- A proposal can target one tenant or none (whole instance).

**Say:** "A proposal is a row. Its test results are rows that point back to it. The legal moves of its life cycle are stored as data, and a trigger refuses any move that is not in that table."
**Source:** `007_proposals.sql`.

#### Slide 13. ER: the data plane and tenancy **[CORE]**
**On the slide:** image zoom 4 and callouts:
- TPC-C tables (CH-benCHmark-derived) plus 3 reference tables.
- 4 tenants own warehouse ranges 1-2, 3-4, 5-8, 9-12.
- 6 big tables are split into **one partition per tenant** (24 partitions).
- 8 tables have **row-level security**.

**Say:** "This is the benchmark schema, not our own design, so the ER here is mostly for context. What is ours is the tenancy: each tenant is a database login with its own range of warehouses, each large table has one partition per tenant, and a security policy stops a tenant from seeing anyone else's rows. Only three foreign keys are declared; the rest of the TPC-C relationships are logical, shown dashed."
**Source:** `dataplane/postgres/initdb/02_schema.sql`.

#### Slide 14. Relational schema and integrity constraints **[CORE]**
**On the slide:** control-plane schema in numbers (counted from the live catalog, partitions excluded):
| | |
|---|---|
| Primary keys | 17 |
| Foreign keys | 16 (several **composite**) |
| Unique constraints | 11 |
| CHECK constraints | 29 |
| Exclusion constraint | 1 |
| Triggers | 9 |
| Functions (security definer) | 18 (14) |
| RLS policies | 46 |

**Visual:** the table, with four examples beside it:
1. `CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$')`
2. `FOREIGN KEY (cluster_id, org_id) REFERENCES clusters (id, org_id)`: a tenant cannot point at another organization's cluster.
3. `EXCLUDE USING gist (cluster_id WITH =, int4range(warehouse_lo, warehouse_hi, '[]') WITH &&)`: two tenants cannot own overlapping warehouse ranges.
4. `role cp.org_role` is an ENUM declared low to high, so `role >= 'OPERATOR'` works.

**Say:** "We pushed as many rules as possible into the database itself, so no application bug can break them. The exclusion constraint is my favourite: a UNIQUE constraint cannot say 'these ranges must not overlap', but an exclusion constraint can."
**Proof you can show live:** the overlapping-range insert fails (section 4, statement 4).
**Source:** `001_foundation.sql`; counts from querying the live control database on 2026-10-14.

#### Slide 15. Normalization and deliberate exceptions **[CORE]**
**On the slide:**
- Tables are in **3NF** in the normal way: each fact is stored once, attributes depend on the key.
- **Deliberate exceptions**, each with a reason:
  - `org_id` repeated on child tables: needed so one policy (`org_id = current org`) isolates organizations, and kept consistent by **composite foreign keys**.
  - `cluster_id` also stored in `query_stats` / `latency_stats` for fast time-range queries (only `org_id` is FK-enforced; the collector is the only writer).
  - `jsonb` columns (`action`, `verdict`, `applied`, `inverse`): a structured document in one column, not queried relationally.
  - `has_password`: a generated (derived) column.
- Data plane follows the TPC-C specification as published (not strictly 3NF, e.g. zip -> city/state).

**Visual:** a small "before/after" or a table with columns *Exception | Why | How it stays consistent*.
**Say:** "Normalization removes redundancy so data cannot contradict itself. Where I broke it on purpose, I can tell you why and what protects consistency."
**Note:** this analysis is mine (I read the schema; the repository does not contain a normalization write-up). Check it with your own reading of the tables before presenting, and be ready to say "the `cluster_id` copy is not enforced by a constraint".

#### Slide 16. DBMS concepts mapped to features (1/3): design and integrity **[CORE]**
| DBMS concept | Where it is used in DBPilot | File |
|---|---|---|
| ER modelling, relational schema | 17 + 13 tables, 3 cardinalities (1:n, m:n, 1:1) | `001`..`008`, `02_schema.sql` |
| Primary keys (uuid, composite, identity) | every table | `001_foundation.sql` |
| Foreign keys, **composite** FKs | `(cluster_id, org_id)` ties child to same organization | `001`, `005`, `007` |
| Referential actions | `ON DELETE CASCADE` (tokens, SLOs), `RESTRICT` (audit, clusters) | `001`, `002` |
| Domain constraints | CHECK (regex, ranges), ENUM, NOT NULL, DEFAULT | `001` |
| Special constraint | EXCLUDE USING gist (no overlapping ranges) | `001` |
| Derived data | GENERATED column `has_password`; the `outcome_ledger` view | `001`, `007` |
| Extended types | `citext` (case-insensitive email), `jsonb`, `int4range` | `001`, `007` |
| Soft delete | `archived_at` (organizations are archived, never deleted) | `008` |

**Say:** "This slide is the checklist: for each concept, where exactly it appears."

#### Slide 17. DBMS concepts mapped to features (2/3): transactions, concurrency, triggers **[CORE]**
| DBMS concept | Where it is used | File |
|---|---|---|
| Transactions (atomicity) | sign-up = organization + user + admin + audit row in one transaction | `004: cp.signup` |
| Transaction-local context | `set_config('app.org_id', ..., true)` vanishes at COMMIT | `controlplane/app/db.py` |
| Row locking | `FOR UPDATE` on the organization row stops two admins demoting each other | `002: guard_last_admin` |
| Queue with `SKIP LOCKED` | two engines never take the same proposal | `engine.py: claim` |
| Serialisation point | partial unique index: one canary per cluster | `007` |
| Non-transactional DDL | `CREATE INDEX CONCURRENTLY` must run in autocommit | `actions.py: run` |
| Triggers | audit (5), append-only (2), last-admin (1), proposal guard (1): 9 in all | `002`, `007` |
| Stored functions | 18, of which 14 `SECURITY DEFINER` | `002`..`008` |
| State machine as data | 19 legal moves in a table, checked by a trigger | `007` |

#### Slide 18. DBMS concepts mapped to features (3/3): security, storage, performance, replication **[CORE]**
| DBMS concept | Where it is used | File |
|---|---|---|
| Authorization (roles, GRANT) | 3 control-plane roles (no superuser, no BYPASSRLS), column-level `GRANT UPDATE (state, ...)` | `003`, `007`, `migrate.py` |
| **Row-level security** | 46 policies; every request confined to one organization; tenants confined to their warehouses | `003`, `02_schema.sql` |
| Least privilege | executor role; event trigger allows only CREATE/DROP INDEX | `executor.sql` |
| Partitioning | range by warehouse (24 partitions); range by day (`query_stats`) | `02_schema.sql`, `005` |
| Indexing | B-tree, composite, **partial**, GiST; per-tenant `CREATE INDEX CONCURRENTLY` | `007`, `actions.py` |
| Query optimisation | `EXPLAIN`, planner cost, HypoPG what-if, `ANALYZE`, `work_mem` | `twin/agent/whatif.py` |
| Statistics / catalog | `pg_stat_statements`, `pg_stat_database`, `pg_stat_replication`, `pg_settings` | `collector.py`, `observe.py` |
| Replication, WAL | streaming replica; delayed standby for the twin; WAL volume as a budget | `twin/agent/pg.py` |
| Connection pooling | PgBouncer; per-role `CONNECTION LIMIT` | `dataplane/pgbouncer`, `actions.py` |
| Audit / logging | trigger-written, append-only audit log; JSON statement log | `002` |
| Multi-tenancy | shared schema + RLS + partition per tenant | `02_schema.sql` |

---

### PART 4. DBMS CONCEPTS IN ACTION

#### Slide 19. Transactions and concurrency **[CORE]**
**On the slide:** three small cases, each with the problem and the mechanism.
1. **Atomic sign-up.** Problem: organization created but no admin. Mechanism: one function, one transaction.
2. **Two admins demote each other at the same instant.** Problem: an organization with no admin. Mechanism: trigger takes `FOR UPDATE` on the organization row, so the two checks run one after the other.
3. **Two engines pick the same job.** Mechanism: `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`.

**Visual:** a timeline for case 2 (two sessions, one waits).
**Say:** "Without locking, both demotions would see 'another admin exists' and both would succeed. With the row lock the second one waits and then sees the truth. For the job queue, SKIP LOCKED means a second worker skips a row another worker is holding, instead of waiting."
**Extra point:** `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block, so the executor insists on an autocommit connection (`actions.run` raises if not). The benefit: building an index does not block writes.
**Honest note:** isolation level is PostgreSQL's default (read committed); the code uses explicit row locks where it needs stronger guarantees.
**Source:** `002_audit.sql` (`guard_last_admin`), `engine.py: claim`, `actions.py: run`.

#### Slide 20. Triggers and the proposal state machine **[CORE]**
**On the slide:** a state diagram: `PROPOSED -> VERIFYING -> (REJECTED | INCONCLUSIVE | AWAITING_APPROVAL | APPROVED | ADVISORY | FAILED)`, `INCONCLUSIVE -> APPROVED (admin) | REJECTED`, `APPROVED -> CANARY -> APPLIED | ROLLED_BACK | FAILED`, `APPLIED -> ROLLBACK_REQUESTED -> ROLLED_BACK | FAILED`. Under it: *12 states, 19 legal moves, stored in a table, enforced by a trigger.*
**Visual:** boxes and arrows; colour REJECTED/ROLLED_BACK red, APPLIED green.
**Say:** "Which moves are legal is data, not code. Even someone typing SQL by hand cannot revive a rejected proposal or skip verification. Also, once created, a proposal's action and settings can never be edited."
**Proof you can show live:** try `REJECTED -> APPLIED`; the database answers `illegal proposal transition REJECTED -> APPLIED` (verified 2026-10-14).
**Source:** `007_proposals.sql` (`proposal_guard`, `proposal_transitions`).

#### Slide 21. Security: two locks on every request **[CORE]**
**On the slide:**
1. The API checks your role.
2. **The database checks it again** with row-level security.
- Role is read from the database on every request, so demoting someone takes effect immediately.
- 3 service roles, none a superuser, none can bypass RLS.
- Passwords: Argon2id. Reset tokens: only the SHA-256 hash is stored.

**Visual:** request -> API (check 1) -> transaction sets `app.org_id` -> table with RLS policy (check 2). Policy text: `USING (org_id = cp.current_org() AND cp.has_role('VIEWER'))`.
**Say:** "Even if the application had a bug, the database would still refuse to show another organization's rows. A request with no organization context sees zero rows of every table."
**Proof you can show live:** as the API role with no context, `select count(*) from cp.proposals` returns 0 (verified).
**Honest limit:** the executor role that applies approved changes must be a member of the table-owning role to build indexes, so in principle it could read rows. PostgreSQL has no narrower privilege; the project states this openly. Mitigation: an event trigger refuses every DDL command from it except CREATE INDEX and DROP INDEX.
**Source:** `003_security.sql`, `docs/DATA_MODEL.md` sections 3, 4, 7.

#### Slide 22. Partitioning and indexing **[CORE]**
**On the slide:**
- One tenant = one **partition** per large table (order_line: 529, 483, 47 and 336 MB for the four tenants on this machine).
- Query for warehouse 6 reads **only** the `t_analytic` partition (partition pruning).
- An index can be built for **one tenant only**, so its write cost falls on that tenant.
- **Partial indexes:** `WHERE state IN (...)` (engine queue), `WHERE state = 'CANARY'` (unique, one per cluster).

**Visual:** parent table `ch.order_line` with four partition boxes, labelled with warehouse ranges 1-2, 3-4, 5-8, 9-12; a row-level-security policy bar above.
**Say:** "Partitioning gives us two things at once: faster queries through pruning, and the ability to build an index for one tenant without taxing the others."
**Proof you can show live:** `EXPLAIN select count(*) from ch.order_line where ol_w_id = 6 and ol_i_id = 100;` as `t_analytic`: the plan touches only `order_line_t_analytic` (verified).
**Source:** `02_schema.sql`; sizes measured 2026-10-14 (the data grows while the workload runs, so say "about").

#### Slide 23. Query optimisation: one real example **[CORE]**
**On the slide (a story in four lines):**
1. Tenant `t_analytic` runs a report that filters `order_line` by item.
2. No index on `ol_i_id`, so every call scans the tenant's whole partition (about 55 ms per call).
3. The planner estimates an index would cut that query's cost by about 99%.
4. Measured: p95 latency of that tenant's analytics class fell to **0.12x to 0.15x** of before (6 production trials); the twin predicted **0.11x to 0.16x** (3 trials).

**Visual:** two `EXPLAIN` plans side by side (Seq Scan vs Index Scan) - take them from the console's Guided demo step 3 ("Show the planner's plan") - and a bar for 1.0x vs about 0.13x.
**Say:** "The tool used the optimiser itself, `EXPLAIN` and hypothetical indexes, to find the problem, and then measured the result. Notice the numbers are ratios, after over before, so smaller is better."
**Source:** `docs/DEMO_GUIDE.md` section 0; `docs/evidence/final/trials_report.txt`.

#### Slide 24. Telemetry, replication and pooling
**On the slide:**
- Collector: stores **differences** of `pg_stat_statements` counters per window (counters only grow).
- Percentiles: from the **statement log**, grouped into transactions by tenant and class.
- **Streaming replica** (read copy) and a **delayed standby** that feeds the twin.
- **PgBouncer** pools connections; tenants have connection limits; tenants log in only through it.

**Visual:** data-flow diagram, primary -> collector -> control DB; primary -> replica; primary -> delayed standby -> two clones.
**Say:** "Everything the console shows comes from these tables; nothing is made up on the way."
**Source:** `docs/DATA_MODEL.md` section 8, `collector.py`, `core/dbpilot_core/pglog.py`.

---

### PART 5. HOW DBPILOT DECIDES

#### Slide 25. The closed menu of actions
**On the slide:** the 9 action types; 6 can be applied, 3 are advice only.
| Applied | Advice only |
|---|---|
| create_index, drop_index, role_setting, instance_setting, analyze, concurrency_cap | no_action, replica_routing, query_rewrite |

An example (this is what the AI produces, never SQL):
```json
{"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_analytic"}
```
**Say:** "Settings are allow-listed and bounded, for example a tenant's `work_mem` may only be between 1 MB and 256 MB. The code that turns an action into SQL also produces its exact undo, which is stored before anything is applied."
**Source:** `core/dbpilot_core/actions.py`.

#### Slide 26. Four checks, cheapest first **[CORE]**
**On the slide:**
| Tier | What | Time |
|---|---|---|
| T0 | static rules (valid? allowed? memory arithmetic: `work_mem x connections` must stay under a quarter of RAM) | milliseconds |
| T1 | planner what-if: would the optimiser even use this index? | seconds |
| T2 | digital twin: replay real traffic on a copy, measure every tenant | minutes (about 17 min for 8 pairs) |
| T3 | canary: apply in production under watch | minutes |

**Visual:** a funnel; each tier can reject early.
**Say:** "Cheap checks first, so a bad idea is thrown out in milliseconds instead of after a 17-minute test."
**Source:** `controlplane/app/engine.py`.

#### Slide 27. The digital twin **[CORE]**
**On the slide:**
1. A **delayed standby** is always "production, a short while ago" (60 s on this stack; the Kubernetes manifest sets 900 s).
2. Copy it twice: **control** (unchanged) and **treatment** (change applied).
3. **Replay the same captured workload** on both, same timing, same tenants.
4. Measure latency **per tenant**. Repeat as several **pairs**, alternating which arm runs first.

**Visual:** standby -> two clones -> replayer -> a table of per-tenant ratios.
**Say:** "It is an A/B test of a database change using real traffic. Both copies are warmed and checkpointed first so neither starts at an advantage."
**Source:** `twin/agent/runner.py`, `replay.py`.

#### Slide 28. The safety gate **[CORE]**
**On the slide:** the rule in plain words:
- The tenant being helped must be **shown** to improve by at least **10%**.
- Every other tenant must be **shown** not to get worse by more than **5%**.
- "Shown" = the **whole confidence interval** is on the right side of the line.
- An interval that straddles the line = **INCONCLUSIVE**, which is **not** a pass.
- Also budgets: write volume (WAL) <= 1.25x, storage <= 2 GiB.

**Visual:** an interval plot with the 0.9 and 1.05 lines; one tenant's bar entirely below 0.9 (benefit shown), one straddling 1.05 (uncertain).
**Say:** "Not finding evidence of harm is not the same as finding evidence of safety. That is the whole point of this gate. One replay pair decides nothing; harm must be seen with each arm running first, in both orders, before we call it harm."
**Technical footnote (if asked):** Student's t interval on the logarithm of each pair's ratio; 95% confidence, corrected for the number of tenant comparisons (Bonferroni) and for looking repeatedly.
**Source:** `core/dbpilot_core/gate.py`.

#### Slide 29. Canary and automatic rollback **[CORE]**
**On the slide:**
- Before applying: record each tenant's baseline p95; store the **inverse** statements.
- Apply (an index for all tenants: one partition first, then the rest).
- Compare each new window to the **contract** (the twin's prediction plus a tolerance; 1.15x if there is none).
- **Roll back automatically** if the contract is broken in 2 of the last 3 windows, if telemetry is lost, if deadlocks spike, or if replica lag is too large.
- If the engine crashes mid-canary, the change is rolled back at restart: *unobserved means unsafe*.

**Visual:** timeline with the contract line and window markers; one case ending in a red "rolled back".
**Source:** `engine.py: canary, rollback_reason, recover`.

#### Slide 30. Who proposes: rules and an AI agent
**On the slide:**
- **Rule-based proposer** (4 rules): spills to disk -> more `work_mem`; missing index (asked of the planner); stale statistics -> `ANALYZE`; burst neighbour -> concurrency cap.
- **LLM agent:** 10 read-only tools + `propose_action`, a 14-turn limit; output is validated; it goes through the **same** pipeline.
- Providers: Anthropic (code path never run live), Groq and Gemini (free tiers).

**Say (honest):** "The agent was run live five times on free-tier models; two runs produced a proposal. That shows the path works. It is not an evaluation of how good an AI is at tuning."
**Source:** `controlplane/app/proposers/`, `docs/REPORT.md` section 8.

---

### PART 6. EVIDENCE

#### Slide 31. How it was tested
**On the slide:**
- Automated tests: control plane, gate and engine (the report documents 200), data plane 36, workload 20, twin 47-54, plus browser tests (Playwright). *Counts are from the report, not re-run today.*
- Experiments are **written down before they are run** (`protocol.md`), raw outputs kept in `docs/evidence/`.
- Everything was measured on **one laptop**, which is not a controlled machine.

**Say:** "I will only show numbers that come from recorded runs, and I will say plainly what they do not prove."

#### Slide 32. Result 1: the index helps its tenant, and the twin saw it **[CORE]**
**On the slide:** table or chart (`t_analytic` analytics class p95, after / before):
| Source | Values |
|---|---|
| Twin (3 trials, 8 pairs each) | 0.126x [0.08, 0.20], 0.114x [0.08, 0.16], 0.156x [0.10, 0.25] |
| Production (6 trials) | 0.12x to 0.15x |

**Visual:** a dot plot, twin intervals above, production points below, vertical line at 1.0.
**Say:** "For this one kind of change the twin and production agree. It is encouraging; it is six production trials and three twin verdicts, so it is not a general validation."
**Source:** `docs/evidence/final/trials_report.txt` (checked against the file).

#### Slide 33. Result 2: the first gate was wrong, so it was fixed **[CORE]**
**On the slide:**
- Test: **30 "A/A" runs** where both copies are identical (the true answer is "no difference").
- First gate: false "harm shown" in **2 of 9** decisions, and never the correct decisive answer.
- Cause: it assumed noise only inside one replay, but two identical runs differ by **5-12%** between runs on this machine.
- Fix: the replay **pair** became the unit of evidence. Re-judged on the same data: **0 false calls in 3,400 resamples**, but also no decisive call.

**Visual:** a before/after pair of boxes.
**Say:** "We found a flaw in our own tool by testing it against a case where we knew the answer. That is the part of the project I am proudest of."
**Source:** `docs/evidence/e1/analysis.txt`, `rejudge.txt`, `docs/evidence/README.md`.

#### Slide 34. Result 3: per-tenant gate vs "overall average" gate **[CORE]**
**On the slide:** one neighbour slowed by a known amount (+20%) was injected into real, noisy measurements. How often each gate would let the change through (target `t_mixed`, averaged over the three neighbours):
| Pairs | Overall-average gate approves | Per-tenant gate approves | Per-tenant gate rejects for harm |
|---|---|---|---|
| 8 | 20% | 0% | 31% |
| 14 | 28% | 0% | 42% |
| 28 | 43% | 0% | 53% |

**Say:** "The average-based gate approves a change that slows a neighbour by 20% in a fifth to over two-fifths of cases, because the gain outweighs the loss in total. The per-tenant gate approved none. The honest price: it also approved nothing harmless. On this laptop, a person still has to decide."
**Be careful to say:** the harm is **injected** into real noise; this is not the twin catching a real regression. With a different target (`t_analytic`) the average gate approved almost nothing at all, so the advantage depends on the workload.
**Source:** `docs/REPORT.md` section 4 Part A; `docs/evidence/final/power.txt` (I checked that the REPORT's figures equal the average of the three per-neighbour rows).

#### Slide 35. Result 4: the canary catches harm, and once raised a false alarm **[CORE]**
**On the slide:**
- Drop the index the tenant depends on: the canary saw that tenant's p95 at **3.69x** its baseline against a limit of **1.15x**, and rolled back **by itself**.
- But in 1 of 3 no-op trials the canary also rolled back a harmless change (a class with very few queries per window has a very unsteady p95). **Not fixed; stated.**

**Visual:** timeline of ratio per window against the contract line.
**Source:** `docs/REPORT.md` sections 2 and 4; `web/e2e/rollback.spec.ts`.

#### Slide 36. What went wrong, and what it taught
**On the slide:**
- **No scenario harmed a neighbour** (indexes, parallelism, load): OLTP latency here is mostly round trips outside the database; class-level p95 hides single-query changes.
- **The laptop is not a measuring instrument:** speed varied 5-24% with CPU frequency; the charger dropping out lined up with anomalies.
- **Bugs found by using the system:** the pooler's own `SET` statements were captured as tenant transactions; the planner what-if judged a tenant's index across *every* tenant's partition; the canary cannot see queueing at the pooler.
- **A finding about PostgreSQL itself:** a batched `UPDATE` with a `FOR UPDATE` CTE crashed PostgreSQL 18.6 backends under concurrency, so it was not used.

**Source:** `docs/REPORT.md` section 3; `docs/DECISION_LOG.md` decision 5 (evidence in `docs/evidence/bench_noise/pg18_batched_update.md`).

#### Slide 37. Limitations: what has not been shown **[CORE]**
**On the slide:**
- That the twin gate **approves** a safe change (on this laptop it never did; every real verdict was INCONCLUSIVE).
- That the twin **catches** a real harmful change (none was found to catch).
- That the twin matches production **in general** (one change type only).
- How often the canary gives false alarms (once in three trials).
- Anything general about the **AI agent** (two proposals).
- Statistical rates with confidence intervals.
- Deployment on real cloud machines (one laptop, Compose; Kubernetes manifests never deployed).

**Say:** "These are the open questions. I would rather tell you than have you find them. The next steps are to run the twin's two copies at the same time on a quiet machine, which should shrink the noise."
**Source:** `docs/REPORT.md` sections 5 and 7.

---

### PART 7. DEMO AND CLOSE

#### Slide 38. Live demo plan **[CORE]**
**On the slide:** four short segments (about 10 minutes):
1. **The database itself** (psql): row-level security, append-only audit, illegal state move, overlapping-range refusal, partition pruning.
2. **The console:** create an organization; it is a real row (refresh, query it).
3. **The guided demo:** the problem, the proposal, the twin's verdict (INCONCLUSIVE, on purpose), human override, canary, applied.
4. **A harmful change undone by the canary.**

Full run-sheet with exact commands: section 4 below.

#### Slide 39. Conclusion and future work **[CORE]**
**On the slide:**
- **Built:** a multi-tenant PostgreSQL platform where database features (RLS, partitioning, triggers, constraints, locking) carry the safety.
- **Shown:** the twin and production agree on a real index's benefit; the average-based gate misses harm the per-tenant gate catches; the first gate had a flaw that testing exposed and fixed.
- **Not shown:** that the twin can approve a safe change on a noisy machine.
- **Next:** run the twin's arms simultaneously on a quiet machine; give the canary a measure of queueing and sample-sized windows; a larger LLM evaluation.

**Say:** "The honest summary: the database design is solid and tested, the idea is promising, and the evaluation says exactly where it stops."

#### Slide 40. Thank you and questions
Repository: https://github.com/sandhiyaa031/Multi-tenant-Cloud-Platform

---

### BACKUP SLIDES (keep hidden; use for questions)

- **B1. Full state-transition table:** the 19 moves from `007_proposals.sql`.
- **B2. Gate parameters:** metric p95; min benefit 10%; max regression 5%; confidence 95%; min 30 samples per key; WAL <= 1.25x; storage <= 2 GiB; stricter margin (x0.5) for changes that are expensive to undo.
- **B3. Related work (not claimed as novel):** Azure SQL auto-indexing (SIGMOD 2019) and Oracle automatic indexing validate changes before keeping them; OnlineTune (SIGMOD 2022) safe tuning; Tempo (VLDB 2016) per-tenant objectives; duet benchmarking (ICPE 2020) for noise. "In a short search no system was found that gates an automatically proposed change on per-tenant non-inferiority in a shared instance; that is the absence of a result in a short search, not proof none exists."
- **B4. Repository map:** `controlplane/` (API, engine, collector, migrations), `core/` (gate, actions, log parsing), `dataplane/` (schema, PgBouncer), `twin/`, `workload/`, `web/`, `docs/`.
- **B5. The per-neighbour injected-harm table** from `docs/evidence/final/power.txt`.
- **B6. Security limits:** executor can in principle read rows; audit log cannot be deleted so organizations are archived.

---

## 4. Live demo run-sheet

**Before the review (once):**
```bash
scripts/review_start.sh
python scripts/demo_admin.py status
```
`status` must end with `READY`. On 2026-10-14 it did (collector 15 s ago, 4 of 4 objectives with data, 16 old proposals waiting).
Sign-in: `DEMO_ADMIN_EMAIL` and `DEMO_ADMIN_PASSWORD` in `.env` (default email `admin@demo.dbpilot.dev`). Do not put the password on a slide.

**Segment 1: the database itself.** I ran every statement below on 2026-10-14; each is inside `BEGIN ... ROLLBACK`, so nothing is changed.
Open psql on the control database:
```bash
docker compose exec control-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```
Then paste, one at a time, saying the sentence after each:
```sql
-- 1. Row-level security: the API role with no organization context sees nothing
BEGIN; SET LOCAL ROLE dbpilot_api; select count(*) from cp.proposals; select count(*) from cp.clusters; ROLLBACK;
-- "The application's own login role sees zero rows until a request sets which organization it acts for."

-- 2. The audit log cannot be changed
BEGIN; UPDATE cp.audit_log SET action='x' WHERE id=(select min(id) from cp.audit_log); ROLLBACK;
-- expect: ERROR: audit_log is append-only

-- 3. A rejected proposal cannot be revived
BEGIN; UPDATE cp.proposals SET state='APPLIED' WHERE id=(select id from cp.proposals where state='REJECTED' limit 1); ROLLBACK;
-- expect: ERROR: illegal proposal transition REJECTED -> APPLIED

-- 4. Two tenants cannot own overlapping warehouses
BEGIN; INSERT INTO cp.tenants (org_id,cluster_id,name,db_role,warehouse_lo,warehouse_hi,profile)
  SELECT org_id,cluster_id,'overlap-test','overlap_test',5,6,'MIXED' FROM cp.tenants WHERE db_role='t_analytic'; ROLLBACK;
-- expect: conflicting key value violates exclusion constraint "tenants_no_overlapping_ranges"
```
Then the data plane:
```bash
docker exec -it dbpilot-dp-primary-1 psql -U postgres -d app
```
```sql
-- 5. A tenant sees only its own rows (this and the next statement run in the data-plane database)
SET SESSION AUTHORIZATION t_analytic;
select min(ol_w_id), max(ol_w_id) from ch.order_line;      -- expect 5 and 8
select count(*) from ch.warehouse;                           -- expect 4 (of 12 in total)
-- 6. Partition pruning
EXPLAIN (COSTS OFF) select count(*) from ch.order_line where ol_w_id = 6 and ol_i_id = 100;
-- expect: Index Scan ... on order_line_t_analytic   (only that partition)
RESET SESSION AUTHORIZATION;
```
Also useful: `\dt cp.*` (list the tables) and `\d cp.tenants` (shows the constraints and the exclusion constraint).

**Segment 2 and 3: the console** (follow `docs/DEMO_GUIDE.md` sections 2 and 3, Route A "stored twin verdict, then your decision", about 8 minutes). I did **not** click through the console myself; the guide matches what the repository's browser tests do, and the stack was READY.

**Segment 4: undo by canary** (`docs/DEMO_GUIDE.md` section 4, about 4 minutes): after the index is applied, "Propose dropping it"; the canary shows about 3.7x against about 1.15x and undoes it.

**After the demo:** `python scripts/demo_admin.py reset`, then `status` should say READY. Do not use `reset --clear-queue` before a Route A demo (it removes the stored twin verdicts).

**If the stack misbehaves:** keep screenshots of each step as a fallback; the table "If something goes wrong" in `docs/DEMO_GUIDE.md` covers the usual causes.

---

## 5. Questions to expect, with honest answers

1. **Why PostgreSQL, why not just application-level checks?** The database enforces the rules even if the application is wrong: constraints, triggers, row-level security. We use both layers.
2. **Why two databases?** One is DBPilot's own memory; the other is the system being tuned. Mixing them would let tuning load disturb the measurements and would mix two security domains.
3. **Is it normalized?** Mostly 3NF; the exceptions are deliberate (slide 15) and each has a reason and a guard. The `cluster_id` copy in the telemetry tables is not constraint-enforced; only the collector writes it.
4. **What isolation level?** PostgreSQL's default, read committed. Where stronger guarantees are needed (last admin, queue) we take explicit row locks.
5. **How do you stop two engines applying the same change?** `FOR UPDATE SKIP LOCKED` on claim, plus the partial unique index "one canary per cluster".
6. **Why uuid keys?** Not guessable, can be generated without coordination; the cost is bigger indexes than integers.
7. **What is a digital twin here?** A delayed standby copied twice, one copy with the change applied, replaying the same captured traffic.
8. **Did it work?** The twin and production agreed on the benefit of one index. The twin never approved a change on real data, because noise between identical runs (5-12%) was larger than the 5% harm margin. All six real twin verdicts were INCONCLUSIVE.
9. **Why not just use a canary?** A canary tests on real users. The twin tests before. How much the twin adds is the research question; on this laptop it cost about 17 minutes against about 75 seconds and approved nothing.
10. **Can the AI do damage?** It can only produce a validated typed action, which goes through the same checks. It cannot run SQL or approve its own proposal.
11. **What if the engine dies mid-change?** At restart a change that was in canary is rolled back, because nothing was watching it.
12. **Why can't organizations be deleted?** The audit log is append-only and references them, so deleting would either destroy the audit trail or break its guarantee. They are archived.
13. **How does `CREATE INDEX CONCURRENTLY` work and why autocommit?** It builds the index without blocking writes by scanning the table in several passes; because of that it cannot run inside a transaction block.
14. **Is this novel?** We do not claim it. Validating a change on a clone is established (Azure SQL, Oracle). What we study is per-tenant gating and an honest measurement of what it adds.
15. **Weakest point?** The measurements come from one noisy laptop; the evaluation says what could not be shown.
16. **Can the executor read tenant data?** In principle yes (it is a member of the owning role, because PostgreSQL has no narrower privilege to create an index). An event trigger limits it to CREATE/DROP INDEX, and the limit is documented.

---

## 6. Glossary (one line each)

- **Tenant:** one customer of the shared database; here a PostgreSQL login role owning a range of warehouse ids.
- **Organization:** a customer of DBPilot itself.
- **Row-level security (RLS):** the database adds a hidden filter to every query so a user only sees permitted rows.
- **Partition:** a table split into physical pieces by a key (here, warehouse id).
- **p95 latency:** the time that 95% of requests finish within.
- **Ratio (treatment / control):** below 1 means faster, above 1 slower.
- **Confidence interval:** the range the true value is likely in; here it must lie wholly on the right side of a threshold.
- **A/A test:** run two identical copies; the true difference is zero, so it measures the noise.
- **Canary:** apply a change to production while watching, ready to undo.
- **Inverse:** the statements that exactly undo a change.
- **Idempotent / append-only:** can be repeated safely / can only be added to, never edited.

---

## 7. Corrections to keep in mind (so nothing is overclaimed)

- Say "tests every change", not "guarantees" or "proves safe".
- Say "a person approves", not "autonomous".
- The twin has **never approved** a change on real data; INCONCLUSIVE is the normal result on this laptop.
- The LLM agent has been run live on free-tier Groq and Gemini models only; the Anthropic path has never run live.
- The "injected harm" experiment uses real noise with a synthetic effect.
- Numbers such as table sizes and proposal counts change as the workload runs and as tests write to the demo database; quote them as "about".

---

## 8. What I verified, and what I did not

**Verified by me on 2026-10-14**
- The stack was running and `demo_admin.py status` printed READY.
- All catalog counts on slide 14 (queried live): 17 base tables in `cp` (partitions excluded), 17 PK, 16 FK, 11 UNIQUE, 29 CHECK, 1 EXCLUDE, 9 triggers, 18 functions of which 14 `SECURITY DEFINER`, 16 of 17 tables with RLS, 46 policies, 41 indexes, 1 ENUM; `ch`: 13 base tables, 24 partitions, 8 RLS policies, tenants 1-2, 3-4, 5-8, 9-12.
- The six database demonstrations in section 4 (outputs as stated).
- The numbers on slides 32-35 against `docs/evidence/final/trials_report.txt`, `power.txt` and `e1/analysis.txt`.
- The gate, engine, actions and rules code were read in full.

**Not verified by me**
- Test counts and that all tests pass (taken from the report; not re-run).
- The console click-through of the demo (follows the repository's guide; not executed by me).
- The LLM live runs (taken from `docs/evidence/agent/` and the report).
- The Canva link you sent; I did not open it. If it is a template, tell me which and I will fit the content to its layouts.
- The normalization analysis on slide 15 is my reading of the schema, not a repository document.

**Uncommitted work:** about 30 files in the working tree (including `docs/DATA_MODEL.md`, the Guided demo page and `scripts/demo_admin.py`) are not on GitHub yet. Commit them before the review if the panel will look at the repository.
