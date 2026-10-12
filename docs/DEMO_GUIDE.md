# Demo guide

How to start DBPilot on one machine, show the whole loop, and shut it down. Commands are run from the repository root in
a terminal (PowerShell or Git Bash). Times are from the development laptop.

## 0. What you need

- Docker Desktop, running. Check with `docker ps`.
- About 16 GB of memory. The stack pins services to CPU cores 0 to 19 (`cpuset` in `docker-compose.yml`); on a machine
  with fewer threads, edit or remove those lines.
- **Keep the laptop on its charger.** Measurements on battery are slower and less steady.
- A file named `.env` in the repository root. It holds passwords and is never committed. On the development laptop it
  already exists. To create one elsewhere, use the variable list in
  [PROJECT_REPORT.md, appendix A.1](PROJECT_REPORT.md#a1-getting-started) and add the laptop profile below.

Laptop profile used for everything recorded in this repository (in `.env`):

```
SEED_SCALE=0.3
SEED_ITEMS=30000
COLLECT_INTERVAL_S=20
TWIN_DELAY_S=60
TWIN_WINDOW_S=55
TWIN_REPETITIONS=1
TWIN_MAX_LOOKS=8
EVAL_RATE_SCALE=0.5
EVAL_POOL_SIZE=8
```

`TWIN_MAX_LOOKS=8` means a twin verdict may use up to eight replay pairs, about 17 minutes. With the default of 3 the
verdict is faster (about 6 minutes) and even the target's benefit is usually not shown.

## 1. Start

First time only (builds images, loads four tenants, registers the demo organization; about 10 minutes):

```bash
docker compose up -d --build
```

```bash
docker compose run --rm dp-seed
```

```bash
docker compose run --rm demo-seed
```

Every other time:

```bash
docker compose up -d
```

Then start the demo traffic. Without it the charts are empty and a canary has no baseline:

```bash
docker compose --profile demo up -d demo-load
```

Check that everything is up (`dp-primary`, `dp-replica` and `control-db` should say "healthy"):

```bash
docker compose ps
```

Wait about two minutes so the collector has a few windows of data.

## 2. Sign in

Open <http://localhost:5173> and choose **Sign in**.

- Email: the value of `DEMO_ADMIN_EMAIL` in `.env` (default `admin@demo.dbpilot.dev`).
- Password: the value of `DEMO_ADMIN_PASSWORD` in `.env`.

Expected: the Overview page. The top bar shows the cluster `demo`, a green badge "collector N s ago", the organization
"Demo Org" and the role "admin".

If the badge is amber or red, the collector is not reporting: run `docker compose ps` and see section 6.

## 3. Tour of the console (5 minutes)

| Page | What to point at |
|---|---|
| Overview | Each tenant's objective and whether it is met; latency per tenant over time |
| Tenants | The four tenants, their warehouse ranges and objectives |
| Workloads | Load per tenant; `t_bursty` spikes periodically |
| Query Intelligence | Top queries per tenant. Find `t_analytic`'s lookup on `order_line` by item: expensive, no index |
| Database Health | Instance counters and settings |

## 4. The main demo: one change through the whole loop (about 25 minutes)

The demo workload has `t_analytic` look items up in `order_line`, which has no index on the item column.

1. **Find the problem.** Agent Console → **Run rule-based proposer**. Expected: one or more proposals are queued, among
   them "Create index on order_line (ol_i_id) for t_analytic". (Alternative: Recommendations → **Propose an action** → Start from
   "Index for one tenant" → Verification "Full" → **Submit for verification**.)
2. **Open the proposal.** Its page shows a five-step status line: Proposed, Verification, a decision step, Canary, In
   production.
3. **Watch verification.** Under "Verification":
   - T0 Static rules: approve, under a second.
   - T1 Planner what-if: approve, "planner cost improves for N of M observed queries".
   - T2 Digital twin: runs for about 17 minutes with eight replay pairs. This is a good time to explain the twin.
4. **Read the twin result.** Expected on this laptop: **INCONCLUSIVE**.
   - `t_analytic/OLAP` (the target): about 0.10× to 0.13×, interval entirely below 0.90, finding "benefits".
   - The other four rows: ratios near 1.0 with intervals such as 0.77 to 1.29, finding "uncertain".
   - The "Ratio in each pair" column shows every pair and which arm ran first.
   - "Pairs to show unharmed" estimates how many pairs each tenant would have needed. In one recorded run it said
     about 12, 17 and 21 for three neighbours and more than 500 for the fourth, against the 8 that were run; it varies
     from run to run.
   - **Download evidence** at the top of the page saves the whole record as a JSON file.

   What to say: the change clearly helps its tenant, and nobody looks hurt, but the twin cannot *show* the others are
   within 5% because two identical replays on this machine already differ by 5 to 12%. So the system does not apply it
   by itself.
5. **Decide as a person.** As admin, type a reason and choose **Override and approve**. (An operator could only reject.)
   The reason is recorded in Audit History.
6. **Canary.** The status moves to Canary. The engine records the baseline, applies the index to production, and watches
   three collector windows (about a minute and a half). Expected: "contract held for every tenant", state **Applied**.
   The canary section lists each window's ratio per tenant.
7. **See the effect.** Overview or Query Intelligence: `t_analytic`'s lookup is now several times faster.
8. **Roll back.** On the proposal page choose **Roll back**. Expected: state **Rolled back**; the index is gone.
9. **Optional, 7 minutes plus verification: the LLM agent.** With a `GROQ_API_KEY` (or `ANTHROPIC_API_KEY`) in `.env`,
   Agent Console → **Run LLM agent**. Its proposal page shows every tool call it made under "Agent trace". On the
   development laptop a recorded live proposal, with its inconclusive twin verdict, is already under Recommendations →
   "Waiting for a decision" (proposed by "agent").
10. **Evidence.** Experiments shows what the twin predicted against what production did. Audit History shows who decided
   what. Canary Deployments opens with the **Canary noise check**: how often the canary's rule would have fired on
   recent telemetry with no change at all.

A shorter version (about 3 minutes) skips the twin: Recommendations → **Propose an action** → "Index for one tenant" →
Verification **Canary only (skips the twin)** → **Submit for verification**. It goes T0 → canary → Applied; then roll it
back.

## 5. Optional: the canary undoing a harmful change by itself (about 5 minutes)

1. Apply the index as in the short version above and leave it applied. On its page, the card "Applied to production"
   shows the statement that was run; copy the index name from it (it starts with `dbp_order_line_`).
2. Recommendations → **Propose an action**. Replace the Action box with
   `{"type": "drop_index", "index_name": "<the name you copied>"}`, set Verification to **Canary only**, and submit.
3. Expected: the canary sees `t_analytic/OLAP` at several times its baseline (3.69× in the recorded run) against a
   contract of 1.15×, and the status becomes **Rolled back by the canary** with nobody clicking anything. The index is back.
4. Roll back the first proposal to leave production clean.

## 6. If something goes wrong

| Symptom | Cause and fix |
|---|---|
| Browser cannot reach the page | Docker Desktop is not running, or the stack is down: `docker compose up -d` |
| Charts empty; Overview says there is no traffic | Start the demo workload (section 1) and wait two collector windows |
| Canary ends in "Failed: no latency baseline" | Same cause: no traffic when the canary started |
| The canary rolls back a change that should have been fine, naming `t_mixed/OLAP` | A known false alarm: that class has only a handful of queries per 20-second window, so its p95 is unsteady (seen once in three no-op trials). Say so, and propose the change again |
| A proposal stays in "Proposed" | The engine is not running: `docker compose logs --tail 30 engine`, then `docker compose up -d --force-recreate engine collector` |
| "Rule-based proposer" finds nothing | The index is already applied (roll the earlier proposal back), or there has been under a few minutes of traffic |
| Agent Console: "the LLM agent is not configured" | Neither `ANTHROPIC_API_KEY` nor `GROQ_API_KEY` is in `.env`. Add one and run `docker compose up -d api` |
| Agent Console: "the model API's quota is used up" | The provider refused the call. A free Groq account allows 200,000 tokens a day per model and one diagnosis uses about 60,000; try again when it has reset, or set `GROQ_MODEL` to another model |
| "Run LLM agent" seems to hang | Expected on a free Groq account: the per-minute limit makes one diagnosis take 6 to 8 minutes. Leave the page open |
| The agent's proposal says "the agent finished without proposing an action" | It used all 14 turns looking. Run it again; this happened in one of the three recorded live runs |
| Signed out with "your session has ended" | Tokens last 8 hours. Sign in again |

## 7. Run the automated checks (optional)

The browser tests need the demo workload running; the first three do not.

```bash
docker compose run --rm --no-deps api python -m pytest -q
```

```bash
docker compose run --rm dp-test
```

```bash
docker compose run --rm twin-test
```

```bash
docker compose run --rm --entrypoint python workload -m pytest -q tests
```

```bash
docker compose run --rm e2e
```

```bash
docker compose run --rm e2e npx playwright test rollback.spec.ts
```

```bash
docker compose run --rm e2e npx playwright test twin.spec.ts
```

The last one runs the whole loop through the twin and takes about 20 minutes.

## 8. Stop and clean up

Stop the demo traffic:

```bash
docker compose --profile demo stop demo-load
```

Stop everything and keep the data (the next start is fast):

```bash
docker compose --profile demo down
```

Remove everything including the loaded data. Only do this if you want to start from nothing; the next start needs the
first-time steps again:

```bash
docker compose --profile demo down -v
```

Before closing a demo, roll back any proposal still in state Applied (Recommendations page), so the next run starts from
the same database.
