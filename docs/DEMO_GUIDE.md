# Demo guide: start, show, reset

Everything here was run on the development laptop, and the browser tests in `web/e2e/` follow the same steps. Numbers
quoted are from recorded runs and vary from run to run; say "about", and read the screen.

## 0. The story in one minute

Several customers (**tenants**) share one PostgreSQL server. Tuning it for one can slow another, and an average hides that.
DBPilot lets a proposer (fixed rules, or an LLM) suggest **one typed change**, then tests it before it touches production:
static rules, a planner check, a **digital twin** that replays real traffic on two copies of the database and measures every
tenant, a **safety gate** that decides from those measurements, and a **canary** that applies the change under watch and undoes
it automatically if a tenant breaks its limit.

| Component | Its one job | It does not |
|---|---|---|
| Proposer (rules or LLM) | suggest one action from a fixed list | run SQL, approve, or apply |
| Digital twin | measure the change, per tenant, on copies | fix anything, or decide |
| Safety gate | turn measurements into APPROVE, REJECT or INCONCLUSIVE; "uncertain" is not safe | run anything |
| Canary | apply under watch; roll back on a breach | judge in advance |

**When the problem occurs, and when DBPilot comes in.** `t_analytic` runs a report query that filters `order_line` by item. No
index covers that column, so every call reads the tenant's whole partition. Nothing is "down"; its objective may still be met.
DBPilot comes in when someone (or something) asks "what should be changed?": its proposer finds the query, the planner
estimates an index would cut its cost by about 99%, and the pipeline decides whether to risk it.

## 1. Start

```bash
scripts/review_start.sh          # first time: also run scripts/make_env.sh if there is no .env
python scripts/demo_admin.py status
```

`status` must end with `READY`. It checks the collector is writing, tenants have traffic, and nothing is left in flight or
applied. If it says `NOT READY`, read its `PROBLEM` lines; `python scripts/demo_admin.py reset` fixes the ones about leftovers.

Open <http://localhost:5173>, choose **Sign in**, and use `DEMO_ADMIN_EMAIL` and `DEMO_ADMIN_PASSWORD` from `.env`
(the email defaults to `admin@demo.dbpilot.dev`). Expected: the Overview with a green "collector N s ago" badge, 4 tenants,
live charts. The sidebar starts with **Guided demo**.

## 2. Part A: organizations are real (3 minutes)

*Claim to prove: an organization created in the browser is stored in PostgreSQL and survives a refresh; one created through the
API appears in the browser; and an organization is not a database tenant.*

1. Sidebar, **Organizations**. Read the panel "How an organization relates to clusters and tenants". Your organization, Demo Org,
   has 1 member, 1 cluster and 4 tenants.
2. Type a name, for example `Review Org`, click **Create organization**. Expected: a green notice "saved in the
   control-plane database", the new row (role admin, 1 member, **0 clusters, 0 tenants**), and the organization in the topbar switcher.
3. **Refresh the page** (F5). The row is still there.
4. Prove it is in the database, in a terminal:

   ```bash
   docker compose exec control-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select slug, name, archived_at from cp.organizations order by created_at desc limit 3"'
   docker compose exec control-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select action, created_at from cp.audit_log where action like '"'"'organizations.%'"'"' order by id desc limit 3"'
   ```

5. The other direction. Create one through the API, as the console does, then refresh the page:

   ```bash
   python scripts/demo_admin.py org-create "Created From The API"
   python scripts/demo_admin.py orgs
   ```

   It appears in the table and in the switcher. The console holds no copy of its own: it reads the list from `GET /api/v1/orgs`.
6. Click **Switch to it** on the new organization, then open **Overview**: "No cluster is registered for this organization yet".
   This is the boundary: creating an organization did not create a cluster or a tenant. (Registering one is under Settings and
   records where an *existing* PostgreSQL deployment is; DBPilot never creates roles or databases.)
7. Back in **Organizations**: enter a new name and **Rename** (the identifier stays), then **Archive this organization**. It is
   kept with its audit trail and nobody can sign in to it; it is archived and not deleted because the audit log is append-only.
   Demo Org's own archive button is disabled and says why (it still has a cluster).

## 3. Part B: the guided demonstration (about 10 minutes, or 25 with a live twin run)

Sidebar, **Guided demo**. Twelve steps, each reading live data; a step that has not happened says so. Choose **one** of the three
routes at step 4.

| Route | Time | What is live | When to use it |
|---|---|---|---|
| **A. Stored twin verdict, then your decision** | about 8 min | the twin's measurement is real but was taken earlier (its date is shown); the human decision, canary and rollback are live | the usual choice: shows every stage with real evidence and no 17-minute wait |
| **B. Full verification, live** | about 25 min | everything, including the digital twin's 8 replay pairs | only if you start it 20 minutes ahead |
| **C. Canary only** | about 5 min | rules, canary and rollback; **no twin** (the page says so) | a time-boxed rehearsal of the canary |

### Steps 1 to 3: sign-in, health, the problem

1. **Sign in.** Shows who you are and your role.
2. **Inspect live health.** Collector age, tenants, objectives met: the same numbers as the Overview.
3. **A tenant with a measurable problem.** For `t_analytic`: its objective, its observed p95, and **where its time goes**: the
   report query, its calls and mean latency (about 55 ms each), and its share of the tenant's time. Click **Show the planner's
   plan**: the planner's own plan, with a sequential scan, is the problem; no amount of watching fixes it.
   - To make the problem breach an objective, click **Set objective to p95 ≤ 50 ms**. This is *customer configuration*, not
     telemetry: observed values are untouched, and the page says so. Watch the badge turn **violating**. Restore it afterwards
     with the button that appears (or `python scripts/demo_admin.py reset`).

### Step 4: ask a proposer

- **Run the rule-based proposer.** Nothing is queued yet: it lists what the rules found (about eight candidates in the demo: more
  memory for a tenant that spills to disk, the missing index for `t_analytic`, statistics refreshes) each with *why*. Choose
  **Queue this one** on "Create index on order_line (ol_i_id) for t_analytic". Only that one is queued.
- **Run the LLM agent** is enabled only if the server has a provider key; the page says which provider and model. It takes
  1 to 2 minutes on Gemini and several on a free Groq tier. It proposes one action by calling read-only tools; if no key is
  configured, the button is disabled and the page says so. Nothing is simulated.
- **Route A:** do not queue anything. Under "Follow this proposal" choose an existing entry whose state reads *inconclusive* and
  whose action is "Create index on order_line (ol_i_id) for t_analytic": it carries a stored twin result.
- **Route B:** keep verification **full** and queue the index; come back when step 8 shows a verdict.
- **Route C:** set verification to **canary only** before queueing.

### Steps 5 to 8: the candidate, the tiers, the twin, the verdict

5. **The candidate action and why.** The typed action (JSON, never SQL), the rationale, and the evidence: for a rule, the planner's
   estimated cost ratio; for an LLM, the provider, the model and the number of tool calls it made. The explanation is a claim; the next steps test it.
6. **Static rules, planner what-if, digital twin.** The lifecycle bar and each tier's decision and time. T0 and T1 take under a second.
7. **Control versus treatment, per tenant.** The twin's result: per tenant and class the p95 in the unchanged copy and in the copy with
   the change, the **ratio**, the **confidence interval**, the ratio in each replay pair, and an estimate of how many pairs it
   would take to show each neighbour unharmed. Typical recorded result for this index: the target at about **0.1×** (interval clearly
   below 0.9×: *benefits*), every neighbour at about 1.0× with an interval such as 0.77 to 1.29 (*uncertain*).
8. **The verdict, and what it means.** The expected verdict on this laptop is **INCONCLUSIVE**: the target's benefit is shown, but no
   neighbour can be shown unharmed at a 5% margin, because two identical replays here already differ by 5 to 12%. The page
   says "Uncertain is treated as not safe" and gives the pairs estimate. **Do not apologise for this; it is the system working.** A gate
   that approved here would be approving without evidence. (An APPROVE has never happened on real measurements in this project.)

### Steps 9 to 12: decision, canary, outcome, audit

9. **The human decision.** For an INCONCLUSIVE proposal the admin may **Override and approve** (type a reason; it is audited);
   an operator may only reject. Do it (Route A or B).
10. **The canary.** The engine records each tenant's p95 as the baseline, applies the index in production, and compares each new
    collector window with a limit (the *contract*): the table shows each window's ratio per tenant against its contract. It
    takes about 90 seconds. Expected: `t_analytic/OLAP` far below its limit (about 0.1×); the other classes near 1.0×.
11. **The outcome.** "Applied: the canary held for every tenant", with the reason recorded.
12. **Audit history and recorded outcome.** The proposal's rows in the append-only audit log (who, what, when, from state to state)
    and the **outcome ledger**: what the twin predicted next to what production showed.

   Back at step 3, the tenant's problem is now measurably gone (its query is several times faster).

## 4. Part C: a harmful change, undone by the canary (about 4 minutes)

At step 11, with the index applied, a notice offers **Propose dropping it**. Click it. The drop is queued with the canary as its only
check, so after the static rules it goes straight to production under watch. Follow steps 10 and 11:

- the canary sees `t_analytic/OLAP` at roughly **3.7× to 3.9×** its baseline against a limit of about 1.15×;
- after two of three windows over the limit it **undoes the change by itself** (the index is rebuilt);
- the outcome reads **"Rolled back by the canary"**, with the reason `contract breached in 2 of the last 3 windows: t_analytic/OLAP`.

Say plainly: this is the canary doing its job *after* production felt the harm for a couple of minutes. Preventing that is what the
twin is for; measuring how much the twin adds is the research question, and on this laptop it is not yet answered.

Then roll back the original index: select the first proposal in the dropdown and click **Roll back** (step 9's panel).

## 5. Part D: the LLM proposer, separately (2 to 5 minutes, optional)

Step 4, **Run the LLM agent**. Expected: the provider and model named next to the button, a busy label, then "The LLM agent
proposed one action". Step 5 shows "An LLM agent (gemini, `gemini-3.8-flash`) made N tool calls" and the proposal page lists every call
and what each returned. Then show that the proposal goes through the **same** steps 6 to 12. The model could not run SQL, approve its
own proposal or skip a tier: its output is a typed action, validated, and bounced back with the reason if it is not one.

Do not say the LLM "fixes" anything. It proposes; the twin measures; the gate decides; the canary watches.

## 6. Part E: roles (2 minutes, optional)

Settings, **Invite** a member with the *viewer* role; with no mail server the console shows the single-use link. Open it in a private
window, set a password, sign in. The viewer sees every page, and has no buttons to propose, decide, invite or archive; the API refuses the
same actions if called directly (HTTP 403). Remove the member afterwards (Settings, Remove).

## 7. Reset and clean up

| Situation | Do |
|---|---|
| Between two run-throughs | `python scripts/demo_admin.py reset` : restores the objective, waits for changes in flight, rolls back anything applied |
| Also clear old proposals waiting for a decision | `python scripts/demo_admin.py reset --clear-queue` : rejects them (records kept). Note this removes the stored twin verdicts from the "waiting" list that Route A uses, so skip it before a Route A demonstration |
| Is it clean? | `python scripts/demo_admin.py status` must end with `READY`; and `docker exec dbpilot-dp-primary-1 psql -U postgres -d app -Atc "select count(*) from pg_class where relname like 'dbp\_%'"` should print 0 |
| End of the day | `scripts/review_stop.sh` |
| Throw everything away | `docker compose --profile demo down -v` (the next start rebuilds and reseeds, about 10 minutes) |

## 8. If something goes wrong

| Symptom | Cause and fix |
|---|---|
| The page cannot be reached | Docker Desktop is not running, or the stack is down: `scripts/review_start.sh` |
| Charts empty, "No tenant traffic" | The demo workload is not running: `docker compose --profile demo up -d demo-load`, then wait two collector windows |
| "Failed: no latency baseline" at the canary | Same cause: no traffic when the canary started |
| A proposal stays *Proposed* | The engine is busy with another verification (a twin run takes about 17 minutes) or not running: `docker compose logs --tail 30 engine` |
| The rules find no index candidate | The index is already applied: `python scripts/demo_admin.py reset` |
| "the LLM agent is not configured" | No `ANTHROPIC_API_KEY`, `GROQ_API_KEY` or `GEMINI_API_KEY` in `.env`; add one and `docker compose up -d api` |
| "the model API's quota is used up" | The provider refused the call (free tiers have daily limits). Wait, or use another provider's key |
| The canary rolls back a change that should have been fine, naming `t_mixed/OLAP` | A known false alarm: that class has only a handful of queries per 20-second window, so its p95 is unsteady (it happened once in three no-op trials). Say so and propose again |
| Signed out with "your session has ended" | Tokens last 8 hours. Sign in again |

## 9. Checks that run without you

```bash
docker compose run --rm --no-deps api python -m pytest -q                       # control plane, gate, engine, proposers
docker compose run --rm dp-test                                                 # data plane
docker compose run --rm --entrypoint python workload -m pytest -q tests         # workload generator
docker compose run --rm twin-test                                               # digital twin
docker compose run --rm e2e                                                     # browser: sign-in, pages, one change through the canary
docker compose run --rm e2e npx playwright test org.spec.ts guided.spec.ts consistency.spec.ts states.spec.ts
docker compose run --rm e2e npx playwright test rollback.spec.ts twin.spec.ts   # automatic rollback; the full loop through the twin (about 20 min)
docker compose run --rm -e RUN_LLM=1 e2e npx playwright test llm.spec.ts        # opt-in: calls the configured LLM provider
```

The browser tests need demo traffic running and a clean state (`status` says `READY`).
