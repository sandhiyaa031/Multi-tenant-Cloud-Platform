# Likely review questions, with accurate answers

Answers are short enough to say aloud. Numbers come from [REPORT.md](REPORT.md) and [evidence/](evidence/README.md).
Where the honest answer is "that was not shown", that is the answer given. Saying it plainly is stronger than being
caught overstating.

## The idea

**1. What does DBPilot do, in two sentences?**
It manages a PostgreSQL server shared by several tenants. When a rule or an AI suggests a tuning change, DBPilot tests it
on a copy of the database with replayed real traffic, checks every tenant separately, and only then applies it under a
watch that can undo it.

**2. What problem is that solving?**
In a shared database a change that helps one tenant can slow another, and an overall metric hides it. Automatic and
LLM-based tuners are evaluated on overall metrics.

**3. What is the research question?**
Whether judging a change tenant by tenant on a twin adds anything over judging the workload as a whole, or over a canary
alone.

**4. Why not just trust the canary?**
A canary tests on real customers. The twin exists to find out first, on a copy. The canary stays as the last line
because a twin is never a perfect copy of production.

**5. Why not let the LLM decide if its change is safe?**
Its explanation is generated text, not a measurement. In DBPilot the proposer has no way to approve or apply anything:
it can only emit one of nine typed actions, and separate code verifies it.

## The design

**6. What is the "digital twin" exactly? Is it a simulation?**
No. It is a real PostgreSQL instance copied from a standby that deliberately lags production. The standby is paused at a
known moment, copied, and the copy is started as an independent database at exactly that moment. The transactions
production ran after that moment are replayed against it.

**7. How do you isolate tenants?**
Each tenant has its own database role, its own partition of each large table, and a row-level security policy that
limits the role to its warehouse range. Tenants hold no privileges on the partitions directly and can only connect
through the pooler.

**8. What is the action space and why is it closed?**
Nine typed actions (create index, drop index, role setting, instance setting, analyze, concurrency cap, replica routing,
query rewrite, no action), validated by `core/dbpilot_core/actions.py`. Closed so that a proposer can never run
arbitrary SQL, and so every action has a known inverse.

**9. How does rollback work?**
When the executor plans an action it also computes the SQL that undoes it, from the current state, and stores it before
applying. Rollback runs that stored inverse: automatically when the canary's contract is broken, on request later, and at
engine start-up for any change left mid-canary.

**10. How does the gate decide?**
For each tenant and class it computes treatment p95 divided by control p95 in every replay pair, then a Student's t
confidence interval on the logarithm of those ratios. The target must have its whole interval below 0.90. Every other
tenant must have its whole interval below 1.05. Harm is declared only if the whole interval is above 1.05 and it was seen
with each arm having run first. Anything else is inconclusive and is not applied.

**11. Why p95 and not the average?**
Objectives are written on the slow end, because that is what users notice. An average can improve while the tail gets
worse.

**12. Why "shown unharmed" rather than "no harm detected"?**
Failing to detect a slowdown in a noisy measurement proves nothing. Requiring the whole interval below the margin is a
non-inferiority test. It is stricter and it is the reason the gate often says inconclusive.

**13. How do you stop many comparisons and repeated looks from producing false approvals?**
Bonferroni correction: the 5% error allowance is divided among the tenant comparisons and again among the looks allowed.

## The evidence

**14. How was it tested?**
294 automated tests against real PostgreSQL instances (191 control plane, gate and engine; 36 data plane; 20 workload;
47 twin), and three browser tests that drive the running stack end to end.

**15. Do passing tests show the idea works?**
No. Tests show the software does what it was written to do. Whether the gate reaches correct decisions on real
measurements is an experimental question. All the tests passed while the first gate was giving false alarms.

**16. What is an A/A test and what did yours find?**
Both arms identical, so any reported difference is false. Thirty such twin runs, 28 valid. The first gate declared harm
in 2 of 9 verdicts and never gave a correct decisive verdict. Its interval came from resampling time buckets inside a
replay and could not see that two sequential replays differ by 5 to 12% by themselves.

**17. What did you change, and is that not tuning the method to the data?**
One change, made on the A/A evidence and then frozen: the gate now judges replay pairs with a t interval and requires
harm in both arm orders. It was a correction of a statistical error (wrong unit of evidence), not a threshold adjusted
until results looked good. The thresholds (10% benefit, 5% harm) were not changed. On the same data the new gate makes
no false call in 3,400 reordered sequences; it also makes no decisive call.

**18. What is your main positive result?**
With a slowdown of known size injected into the real A/A measurements, the aggregate gate approved a change that slowed
one neighbour by 20% in 20 to 43% of sequences, depending on the replay budget. The per-tenant gate approved none, and
with eight or more pairs rejected 83 to 92% of 50% slowdowns.

**19. Is that result real or simulated?**
Semi-synthetic. The noise is real measurement noise from the twin; the harm was added by multiplying one tenant's
latencies. It shows how the two decision rules behave. It does not show that the twin reproduces production effects.

**20. Did the twin ever catch a real harmful change?**
No. No change in the action space was found that harms a neighbour on this stack, so there was nothing real to catch.
This is stated as not shown.

**21. Why did your "trap" scenarios not harm anyone?**
Three reasons found by measurement. The parallelism changes did nothing because the row-security helper functions are
marked parallel-unsafe, so tenant queries never run in parallel. One more index did not measurably slow writers because
about 85% of an OLTP request's time here is round trips outside the database. And raising load to create contention hit a
lock convoy that collapses the run.

**22. Did the twin gate ever approve a change?**
Not on real measurements. On the laptop, identical arms differ by 5 to 12% per pair, so no neighbour can be shown within
5% in a practical number of pairs. All six twin-verified trials in the closing evaluation ended inconclusive, after
eight pairs each. In the injected analysis it
approved 0%, including the harmless change.

**23. Then is the gate useful?**
On this machine it is safe and not useful for autonomy: it blocks everything and asks a person. Whether steadier
hardware and running the two arms at the same time make it decisive is the open question. That is the honest state.

**24. Does the twin agree with production?**
For the one case measured, the index's effect on its target: the twin said 0.11× to 0.16× in three verdicts, and
production measured 0.12× to 0.15× in six trials. That is one kind of action, not a validation.

**25. Has the canary's automatic rollback been seen working?**
Yes, live in a browser test: dropping an index a tenant depended on took that tenant to 3.69× its baseline against a
1.15× contract, and the engine restored the index without anyone asking. One mechanism, one run.

**25b. Does the canary ever get it wrong?**
Yes. In the closing trials it rolled back a statistics refresh, which changes nothing, once in three tries. It saw one
tenant's analytical class at 1.27× baseline while the load generator measured 1.03×. A 20-second window holds only a
handful of that tenant's queries, and a p95 over a handful of samples is unsteady. Longer windows or a larger minimum
sample count would be the fix; it was not made.

**25c. Can you tell in advance whether a canary is trustworthy?**
Partly. The Canary Deployments page has a noise check: it replays the canary's rule over recent telemetry in which
DBPilot changed nothing and counts what it would have rolled back. On the three hours containing the closing trials it
flagged 28% of stretches, mostly the class with about 7 transactions per window. That period includes workload restarts
and other load on the laptop, so it is an indication that the rule is fragile here, not a measured rate.

**26. Does the LLM agent work?**
The agent loop, its tools and its path through the API are tested against a scripted stand-in model. It has never
completed a run against a live model. No key was available during the project; afterwards a Groq path was added and
the one attempt was refused because the free account's daily quota was already spent. Nothing is claimed about the
quality of its proposals.

## Limitations

**27. What are the main limitations?**
One laptop whose speed varies 5 to 24% under load; all three planes on that one machine; a 0.3-scale dataset at half
rate; no real cross-tenant harm found; the LLM never run live; few trials, so no rates with useful confidence intervals;
the canary cannot see waiting at the connection pooler and raised one false alarm in three no-op trials.

**28. Why did you not use a cloud machine?**
No paid machine was available. A protocol and runner for that measurement exist in `tools/aa/` and were never run.

**29. What would you do next?**
First, run the two twin arms at the same time on separate cores, as duet benchmarking does, since the noise seen is
mostly drift between sequential arms. Second, repeat the A/A test on a steady machine. Only then run the scenario suite.
Also: find a real cross-tenant harm, give the canary a client-side measure, and run the LLM agent live.

**30. What are known weaknesses you did not fix?**
Two in the canary. It measures latency inside the database, so a concurrency cap that makes requests queue at the pooler
slows the tenant without the canary seeing it. And its windows are too short for low-traffic classes, which produced one
false rollback (question 25b).

## Novelty and related work

**31. What is genuinely new here?**
Be careful with this one. Validating an automatic change and reverting on regression is not new (Azure SQL automatic
indexing, Oracle automatic indexing). Replaying captured workload on a test system is not new (Oracle Database Replay).
Safe tuning with a constraint is not new (OnlineTune). What this project set out to add is gating an automatically
proposed change on **per-tenant** non-inferiority in a shared instance, and measuring what that adds. A focused search
found no system doing that; the search was not systematic, so **no novelty is claimed**, only that none was found.

**32. What is the closest related work?**
Das et al., SIGMOD 2019 (Azure SQL auto-indexing: validate and revert at scale); OnlineTune, SIGMOD 2022 (safety
threshold while tuning a live database, on the instance as a whole); Tempo, VLDB 2016 (per-tenant objectives in
multi-tenant parallel databases); and Bulej et al., ICPE 2020 (duet benchmarking), which explains what the twin's
measurements were missing. Links are in [PROJECT_REPORT.md](PROJECT_REPORT.md#8-references).

**33. What did you learn that you did not expect to?**
That the hard part was the measurement, not the system. And that an experiment designed only to catch your own tool
being wrong (the A/A test) was the most valuable one run.

## Practical

**34. What would it take to run this in production?**
Separate machines for the three planes; concurrent twin arms; a controlled-machine validation of the gate; a real
onboarding path for clusters (the collector and engine currently reach one data plane with credentials fixed at start);
and the Kubernetes manifests in `deploy/k3s/`, which are written and have never been deployed.

**35. How long does a verdict take, and is that acceptable?**
About 17 minutes for eight pairs on the laptop, against about 75 seconds for a canary alone. For an index that lives for
months that is acceptable; for the gate to be worth it, it has to reach a decision in that time, which here it does not.
