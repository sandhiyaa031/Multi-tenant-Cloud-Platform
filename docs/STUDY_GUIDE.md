# DBPilot study guide

Read this first. It explains the project from the problem it solves to what was actually found, in the order you would
explain it to someone else. It assumes you know what a database table and a SQL query are, and nothing more.

The other documents in the study pack:

| Document | Use it for |
|---|---|
| [KEY_CONCEPTS.md](KEY_CONCEPTS.md) | Looking up a term you do not know |
| [ARCHITECTURE_WALKTHROUGH.md](ARCHITECTURE_WALKTHROUGH.md) | Finding the code for a component and seeing what calls what |
| [DEMO_GUIDE.md](DEMO_GUIDE.md) | Starting the system and showing it to someone |
| [DEFENSE_QUESTIONS.md](DEFENSE_QUESTIONS.md) | Practising answers for a review or viva |
| [REPORT.md](REPORT.md) | The closing report: what was measured and what was not |
| [PROJECT_REPORT.md](PROJECT_REPORT.md) | The long report, with diagrams, references and setup details |
| [LEARNING_GUIDE.md](LEARNING_GUIDE.md) | Every technology used, explained in depth |

## 1. The problem in one paragraph

A company that sells software to many customers usually keeps all of them in one PostgreSQL database server, because a
server per customer costs too much. Each customer is a *tenant*. They share the server's memory, processor cores and
settings. So a change made to speed up one tenant can slow another: an index that makes one tenant's reports fast makes
every write to that table slightly slower for everyone. If you only look at the server's overall speed, the change looks
like a win, and the tenant who got slower is hidden inside the average.

## 2. Why this matters now

Tuning used to be done by a database administrator. Increasingly it is done by software, and recently by language
models that read statistics and suggest changes. These systems are tested on one workload with one overall number. In a
shared server that number can go up while one tenant gets worse. A language model also cannot be trusted to judge its
own suggestion: its explanation is text, not a measurement.

## 3. The idea

Treat every suggested change as a guess that has to be tested before it touches the real database.

1. A **proposer** suggests a change. It can be a set of fixed rules or a language model. It can only pick from a fixed
   list of nine kinds of change (create an index, change a setting for one tenant, and so on). It never writes SQL.
2. **T0, static rules.** Is this a valid change, for a real tenant, within safe limits? Takes milliseconds.
3. **T1, planner what-if.** For an index: would PostgreSQL even use it? Asked with a pretend index. Takes seconds.
4. **T2, digital twin.** Copy the database. Replay the last minute of real traffic on two copies, one unchanged
   (*control*) and one with the change (*treatment*). Compare how long each tenant's requests took. Repeat several
   times. Takes minutes.
5. **The gate** decides from those measurements. The tenant being helped must be *shown* to get faster, and every other
   tenant must be *shown* not to get slower. "Could not tell" counts as not safe.
6. **T3, canary.** If approved, apply the change to the real database, watch every tenant for a few minutes, and undo it
   automatically if any tenant gets slower than a limit.
7. **Outcome ledger.** Record what the twin predicted and what production did, so the two can be compared later.

Four rules hold everywhere: the proposer never runs SQL; the proposer cannot approve its own proposal; every tenant is
measured, not only the one being helped; uncertain is not safe.

## 4. The research question

> Does judging a change **tenant by tenant** on a twin add anything over judging the workload **as a whole**, or over a
> canary alone?

"Per-tenant gate" and "aggregate gate" are the two ways of judging. The aggregate gate is what single-database tuners
effectively do, and it is in the code only as the comparison.

## 5. What was built

Three groups of services, called planes, run together with Docker Compose:

| Plane | What is in it | Job |
|---|---|---|
| Control plane | Web console, API, its own database, telemetry collector, verification engine | Decides |
| Data plane | PostgreSQL primary and replica, connection pooler, four demo tenants, workload generator | Is the thing being protected |
| Experimentation plane | Twin node agent with its clones | Tests changes |

There are 275 automated tests and three browser tests that drive the whole running system.
[ARCHITECTURE_WALKTHROUGH.md](ARCHITECTURE_WALKTHROUGH.md) has the map.

## 6. What happened when it was measured

This is the part to understand best, because it is what a reviewer will ask about. Everything was measured on one
laptop.

**a. The designed "traps" did not work.** The test scenarios included changes meant to help one tenant and hurt
another. None of them measurably hurt anyone. The reasons were specific: tenant queries never run in parallel because of
how the row-security functions are declared, so the parallelism changes did nothing; and one extra index is too small a
cost to see when most of a request's time is network round trips.

**b. The laptop is not steady.** Its speed changed by 5 to 24% under load, following processor frequency. The same
workload used very different amounts of CPU on different days.

**c. The first gate was wrong.** In an *A/A experiment* both copies are identical, so any difference the system reports
is false. The first gate reported harm in 2 of 9 verdicts. It measured the scatter of requests inside one replay and
could not see that two replays run one after the other differ by themselves (5 to 12% on this laptop).

**d. The gate was redesigned once.** It now treats one control replay plus one treatment replay as a *pair*, and builds
its confidence interval from how much the pairs disagree. Harm only counts if it is seen with each copy having run
first. On the same data it makes no false call. It also never reaches a decisive verdict there.

**e. A test with injected harm.** Since no real harmful change was found, a slowdown of known size was added by
arithmetic to one tenant in the real A/A measurements. The aggregate gate approved a change that slowed a neighbour by
20% in 20 to 43% of cases. The per-tenant gate approved none, and rejected most slowdowns of 50% or more. This is the
project's main positive result, and it is *semi-synthetic*: the noise is real, the harm is not.

**f. Real changes through the real system.** A genuinely useful index and a do-nothing change were each run with no
verification, with a canary only, and with the twin. The twin saw the index's benefit clearly and agreed with production
about its size. It could not show the other tenants unharmed, so it ended INCONCLUSIVE every time, and the change was
not applied without a person (18 trials, all valid). Numbers are in [REPORT.md](REPORT.md) section 4.

One result in those trials was not expected: the canary once rolled back the do-nothing change. It judges 20-second
windows, and for a tenant with only a handful of queries per window the p95 jumps around by chance. So the canary has
false alarms too.

**g. The canary works live.** Dropping an index a tenant needed made that tenant 3.69 times slower against a limit of
1.15, and the canary undid it without anyone asking.

## 7. The honest summary

| Shown | Not shown |
|---|---|
| The whole loop runs, in a browser, on a real stack | The twin gate approving a safe change (on this laptop it cannot) |
| The canary rolls back a harmful change by itself (and, once, a harmless one) | The twin catching a real harmful change (none was found) |
| A gate that resamples inside a replay gives false alarms; one that judges pairs does not | That the twin predicts production in general (it matched for one index) |
| With injected harm, the aggregate gate approves what the per-tenant gate refuses | Anything about the language-model proposer (never run against a live model) |

The sentence to remember: **"we did not see any harm" and "we showed there was none" are different claims**, and most of
the difficulty of the project is in the gap between them.

## 8. Suggested order for tomorrow

1. This guide, then [KEY_CONCEPTS.md](KEY_CONCEPTS.md) for any term that was unclear. (45 minutes)
2. [DEMO_GUIDE.md](DEMO_GUIDE.md): start the stack and click through one proposal. Seeing it makes the code easier.
   (45 minutes, most of it waiting)
3. [ARCHITECTURE_WALKTHROUGH.md](ARCHITECTURE_WALKTHROUGH.md) with the code open. Read the files in this order:
   1. `core/dbpilot_core/actions.py`: what a change is.
   2. `controlplane/migrations/007_proposals.sql`: the life of a proposal as database states.
   3. `controlplane/app/engine.py`, function `verify`, then `canary`: the loop itself.
   4. `core/dbpilot_core/gate.py`, functions `compare` and `decide`: the decision.
   5. `twin/agent/runner.py` and `twin/agent/pg.py`: how a clone is made and replayed.
   6. `controlplane/app/proposers/rules.py`: how a proposal is found.
   7. `web/src/pages/Optimise.tsx`, component `ProposalPage`: how it is shown.
4. [REPORT.md](REPORT.md), then the evidence index [evidence/README.md](evidence/README.md).
5. [DEFENSE_QUESTIONS.md](DEFENSE_QUESTIONS.md): cover the answers and try each one aloud.
