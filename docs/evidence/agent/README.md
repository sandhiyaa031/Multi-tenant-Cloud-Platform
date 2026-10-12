# The LLM agent, live: four runs on 2026-10-12

The first times the agent of `controlplane/app/proposers/agent.py` ran against a real model. They were made after the
closing evaluation, with no protocol written beforehand, on open-weight models served by Groq's free tier
(8,000 tokens a minute, 200,000 tokens a day per model). They show that the path works end to end. Three runs of two
small models say nothing about how good an LLM's proposals are in general, and nothing about any other model.

Each run was one call to `POST /clusters/{id}/diagnose` with `source: "agent"` and no operator note, on the demo
cluster under the demo workload (`workload/profiles/demo.json`), laptop profile.

| File | Run | Outcome |
|---|---|---|
| `run1.json` | `openai/gpt-oss-120b`, 05:09 UTC. 14 model calls, 361 s of which 339 s waiting for the per-minute limit, 55,909 input and 2,025 output tokens | **No proposal.** The model investigated (objectives, top queries per tenant, table profile, a plan, a what-if for an index on `order_line(ol_i_id)` for `t_analytic`, history) and used its fourteenth and last turn on another tool call. Stored as `no_action` with `escalate` |
| `run2.json` | Same model, 05:19 UTC, after the two fixes below | **Refused by the provider** on the twelfth model call: the account's daily quota was used up. Nothing was stored and the trace of the eleven calls before it was lost |
| `run3.json` | `openai/gpt-oss-20b` (a smaller model, with its own daily quota), 05:27 UTC. 12 model calls, 429 s of which 406 s waiting, 60,085 input and 5,090 output tokens | **A proposal:** an index on `order_line (ol_w_id, ol_d_id, ol_o_id, ol_delivery_d)` for `t_mixed` |
| `run4_partial.json` | `openai/gpt-oss-120b` again, 09:09 UTC, with every fix in place | **Refused by the provider** on the ninth model call: the daily quota was used up again (the morning's runs still counted against it). Nothing was stored. The eight calls before it were kept in the API log, which is what this file is: tenants, objectives, latency, `t_mixed`'s top queries, two table profiles and a plan, with no observation asked for twice and the plan tool working on the statement it had failed on |
| `run3_verification.json` | What DBPilot did with that proposal | T0 approve. T1 approve (planner cost improves for 1 of 15 observed queries). T2, 8 replay pairs, 10,991 transactions, 1,055 s: **INCONCLUSIVE**. Not applied |
| `rules_same_telemetry.json` | The rule-based proposer on the same telemetry, minutes later, nothing stored | 8 proposals: sort memory for `t_mixed`, an index on `order_line(ol_i_id)` for `t_analytic` and for `t_mixed`, a statistics refresh for five partitions |

## What run 1 taught, and what was changed

The adapter for Groq showed the model only the first 300 characters of every earlier tool result, to stay inside the
per-minute limit. The objectives of four tenants take about 600 characters, so the model saw one tenant's and asked for
the same observation again: three times for the objectives, twice for the table profile. It was also never told
how many turns it had. It ran out while still looking.

Two changes followed, both covered by tests against a scripted server:

- all tool results in a request share one size budget, spent from the newest back, so recent results stay whole
  (`groq_client.py`);
- the task now states the turn budget, and the last tool results the model will get come with a notice that the next
  turn must decide (`agent.py`). This applies to every provider.

After run 2 lost its trace, a run that the model API cuts short now writes what the agent had done to the API log.

Runs 1 and 3 therefore differ in two things at once, the model and these changes. They are not a comparison of the
models.

## What run 3 shows

- The model's numbers were taken from the tools: planner cost 44,618 to 13,984 for one query, an index the planner sized at about 63 MB (it took 18 MB on the clone),
  no objective violated.
- Its reasoning joined two different queries. It named `t_mixed`'s temporary-file spill as the problem, and proposed an
  index that the what-if showed to help another query, one that does not spill. The rule-based proposer answers the
  spill with sort memory for that tenant.
- It repeated itself less than run 1 but still asked twice each for the objectives, the top queries, the table profile
  and the same what-if.
- One tool failed on it: `explain_query` cannot plan a statement whose placeholders sit inside `sum(CASE ...)`
  ("function sum(text) does not exist"). That was a fault of the tool, found by this run and fixed afterwards (`twin/agent/whatif.py`); the planner what-if had been silently skipping such statements for the same reason.
- The twin did not confirm the proposal. Its own target, `t_mixed/OLAP`, measured 0.91x with an interval of 0.76 to
  1.08: a benefit of 10% was not shown. The other classes measured 0.96x to 1.06x with intervals as wide as 0.77 to
  1.46. The proposal waits for a person, as an inconclusive one does. A plausible, well-worded rationale was not enough
  to reach production, which is the point of the design.

## What these runs do not show

- How often a model reaches a decision, or how good its proposals are: there is one completed proposal.
- Anything about a model that sees whole tool results. The free tier forced results to be shortened.
- Anything about the Anthropic path, which has still never been run against the live service.
