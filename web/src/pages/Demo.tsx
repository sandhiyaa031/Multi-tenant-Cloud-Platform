// The guided demonstration: one real change, end to end, in the order a reviewer needs to see it.
//
// Nothing on this page is scripted. Every figure is read from the API, every button calls the same
// endpoint the rest of the console uses, and a step that has not happened yet says so instead of
// showing an example. The page only arranges the stages and explains what each component does.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, can, describeAction, fmt, useApi, type Cluster, type Proposal, type ProposalDetail } from "../api";
import { NeedCluster, useAuth, useCluster } from "../App";
import { Badge, Card, Load, PageHead, Stat, Stepper } from "../ui";
import { CanaryView, DecisionPanel, IN_FLIGHT, Steps, TwinRun } from "./Optimise";
import { lifecycle, rejectedAt, STAGES, TIER_WORDS, verdictMeaning } from "../lifecycle";

const TARGET_ROLE = "t_analytic";          // the demo workload's tenant with the missing index
const DEMO_OBJECTIVE_MS = 50;              // a tighter p95 objective, so the issue shows up as a violation
const RESTORED_OBJECTIVE_MS = 2000;        // the objective the demo organization is registered with

function Step({ n, title, done, children, sub }: { n: number; title: string; done?: boolean; sub?: ReactNode; children: ReactNode }) {
  return (
    <Card title={`${n}. ${title}`} sub={sub} action={done === undefined ? undefined : <Badge tone={done ? "good" : ""}>{done ? "done" : "waiting"}</Badge>}>
      {children}
    </Card>
  );
}

export function Demo() {
  return <NeedCluster>{(c) => <DemoFor cluster={c} />}</NeedCluster>;
}

function DemoFor({ cluster }: { cluster: Cluster }) {
  const { me } = useAuth();
  const { tenants } = useCluster();
  const operator = can(me?.org.role, "OPERATOR");
  const [params, setParams] = useSearchParams();
  const select = (id: string) => setParams({ p: id }, { replace: true });

  const slo = useApi<any[]>(`/clusters/${cluster.id}/slo-status?minutes=15`, 8000);
  const instance = useApi<any[]>(`/clusters/${cluster.id}/instance?minutes=15`, 15000);
  const agent = useApi<{ configured: boolean; provider: string | null; model: string | null }>("/agent/status");
  const proposals = useApi<Proposal[]>(`/proposals?cluster_id=${cluster.id}&limit=100`, 5000);

  // ── the tenant to look at: the demo workload's, or the one furthest over its objective ──
  const target = useMemo(() => {
    const byRole = tenants.find((t) => t.db_role === TARGET_ROLE);
    if (byRole) return byRole;
    const worst = [...(slo.data ?? [])].sort((a, b) => b.observed_ms / b.threshold_ms - a.observed_ms / a.threshold_ms)[0];
    return tenants.find((t) => t.db_role === worst?.tenant_role) ?? tenants[0];
  }, [tenants, slo.data]);
  const targetSlo = (slo.data ?? []).find((s) => s.tenant_role === target?.db_role && s.query_class === "OLAP") ?? (slo.data ?? []).find((s) => s.tenant_role === target?.db_role);
  const topQuery = useApi<any[]>(target ? `/clusters/${cluster.id}/top-queries?tenant_id=${target.id}&minutes=15&limit=1` : null, 15000);
  const [plan, setPlan] = useState<{ queryid: string; text: string } | null>(null);

  // ── which proposal is being followed ─────────────────────────────────────────
  const list = proposals.data ?? [];
  const chosen = params.get("p") ?? list.find((p) => p.action.tenant_role === target?.db_role && p.source !== "agent")?.id ?? list[0]?.id ?? null;
  const detail = useApi<ProposalDetail>(chosen ? `/proposals/${chosen}` : null, 5000);
  const d = detail.data && detail.data.id === chosen ? detail.data : undefined;
  const twin = d?.twin_runs.at(-1);
  const audit = useApi<any[]>(d ? "/audit?limit=200" : null, 10000);
  const ledger = useApi<any[]>(d ? `/clusters/${cluster.id}/ledger?limit=200` : null, 15000);

  // ── actions ──────────────────────────────────────────────────────────────────
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [mode, setMode] = useState<"full" | "canary_only">("full");
  const fail = (e: Error) => { setMessage({ ok: false, text: e.message }); setBusy(null); };
  // The rules are run first without queueing anything: every queued proposal is verified one after another,
  // and a twin verdict takes minutes, so the person chooses which candidate to follow.
  const [candidates, setCandidates] = useState<any[] | null>(null);
  const preview = () => {
    setBusy("preview"); setMessage(null);
    api<any[]>(`/clusters/${cluster.id}/diagnose/preview`, { method: "POST" })
      .then((found) => { setCandidates(found); setBusy(null); if (found.length === 0) setMessage({ ok: true, text: "The rules find nothing to propose on the current telemetry." }); })
      .catch(fail);
  };
  const queue = (action: Record<string, any>) => {
    setBusy("queue"); setMessage(null);
    api<Proposal[]>(`/clusters/${cluster.id}/diagnose`, { method: "POST", body: { source: "rule", only_action: action, verification: mode, gate_mode: "per_tenant", auto_approve: false } })
      .then((made) => {
        proposals.reload(); setBusy(null);
        if (made[0]) { setCandidates(null); select(made[0].id); setMessage({ ok: true, text: mode === "full" ? "Queued. The engine starts with the static rules and the planner, then replays on the digital twin; steps 6 to 8 fill in as it goes." : "Queued with the canary as its verification; it goes to production under watch after the static rules." }); }
        else setMessage({ ok: false, text: "The rules no longer make that proposal: the telemetry changed since the preview. Ask again." });
      }).catch(fail);
  };
  const askAgent = () => {
    setBusy("agent"); setMessage(null);
    api<Proposal[]>(`/clusters/${cluster.id}/diagnose`, { method: "POST", body: { source: "agent", verification: mode, gate_mode: "per_tenant", auto_approve: false } })
      .then((made) => {
        proposals.reload(); setBusy(null);
        if (made[0]) { select(made[0].id); setMessage({ ok: true, text: "The LLM agent proposed one action. Its tool calls are on the proposal page; steps 5 onwards test its claim." }); }
        else setMessage({ ok: true, text: "The LLM agent found nothing to propose." });
      }).catch(fail);
  };
  const setObjective = (ms: number) => {
    if (!target) return;
    setBusy("objective");
    api(`/tenants/${target.id}/slos`, { method: "PUT", body: { query_class: targetSlo?.query_class ?? "OLAP", percentile: targetSlo?.percentile ?? 95, threshold_ms: ms } })
      .then(() => { slo.reload(); setBusy(null); setMessage({ ok: true, text: `${target.db_role}'s objective is now p${targetSlo?.percentile ?? 95} under ${fmt.ms(ms)}. Observed values were not touched.` }); })
      .catch(fail);
  };
  const showPlan = (queryid: string) => { setBusy("plan"); api<any>(`/clusters/${cluster.id}/queries/${queryid}/explain`)
    .then((r) => { setPlan({ queryid, text: r.explained ? r.plan : `The planner could not explain it: ${r.error}` }); setBusy(null); }).catch(fail); };
  // The harmful-change scenario: take away the index the last change built, and let the canary notice.
  const appliedIndex = d?.state === "APPLIED" ? d.canary?.applied?.map((s: string) => s.match(/INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?(\S+) ON/)?.[1]).find(Boolean) : undefined;
  const proposeDrop = () => {
    setBusy("drop"); setMessage(null);
    api<Proposal>(`/clusters/${cluster.id}/proposals`, { method: "POST", body: {
      action: { type: "drop_index", index_name: appliedIndex },
      rationale: "Demonstration of a harmful change: remove the index the previous change built, so the tenant that relied on it slows down. Verification is the canary only, so the canary has to notice.",
      verification: "canary_only", gate_mode: "per_tenant", auto_approve: false } })
      .then((p) => { proposals.reload(); select(p.id); setBusy(null); setMessage({ ok: true, text: "Queued with the canary as its only check, so after the static rules it goes straight to production under watch. Follow it in steps 10 and 11." }); })
      .catch(fail);
  };
  useEffect(() => { setPlan(null); }, [target?.id]);

  const last = instance.data?.at(-1);
  const age = last ? Math.max(0, (Date.now() - new Date(last.window_end).getTime()) / 1000) : null;
  const metCount = (slo.data ?? []).filter((s) => !(s.windows > 0 && s.observed_ms > s.threshold_ms)).length;
  const violating = !!targetSlo && targetSlo.windows > 0 && targetSlo.observed_ms > targetSlo.threshold_ms;
  const q = topQuery.data?.[0];
  const lc = d ? lifecycle(d) : null;
  const verdict = twin?.verdict;
  const tight = DEMO_OBJECTIVE_MS;
  const mineAudit = (audit.data ?? []).filter((a) => d && (a.entity_id === d.id || a.detail?.new?.id === d.id)).slice(0, 12);
  const ledgerRow = (ledger.data ?? []).find((r) => d && r.proposal_id === d.id);
  const stoppedAt = d ? rejectedAt(d) : null;                       // the tier that refused it, if one did
  const worstUncertain = verdict ? Math.max(0, ...Object.values<any>(twin.planning ?? {}).map((x: any) => (x.reachable && x.needed != null ? x.needed : 0))) : 0;

  return (
    <div className="stack">
      <PageHead title="Guided demo">One real change from a tenant's problem to its outcome. Every number here is read from the live system; a step that has not happened yet says so.</PageHead>
      {message && <div className={message.ok ? "notice" : "error"} role={message.ok ? "status" : "alert"}>{message.text}</div>}

      <Card title="Who does what" sub="four components with four different jobs; none of them does another's">
        <div className="flow">
          <div><strong>Proposer</strong><div className="muted">rules or an LLM reads telemetry and suggests one typed action. It cannot run SQL or approve anything.</div></div>
          <div><strong>Digital twin</strong><div className="muted">replays real traffic on two copies of the database, one with the change, and measures each tenant. It fixes nothing.</div></div>
          <div><strong>Safety gate</strong><div className="muted">turns those measurements into APPROVE, REJECT or INCONCLUSIVE. Uncertain is not safe.</div></div>
          <div><strong>Canary</strong><div className="muted">applies the approved change in production, watches every tenant, and undoes it automatically if a limit is broken.</div></div>
        </div>
      </Card>

      <Step n={1} title="Sign in" done sub="authentication and organization-level authorization">
        <p className="secondary">You are <strong>{me?.email}</strong>, {me?.org.role.toLowerCase()} of <strong>{me?.org.name}</strong>. Every request carries your signed token; the API checks your role and PostgreSQL's row-level security checks it again. See <Link to="/app/organizations">Organizations</Link>.</p>
      </Step>

      <Step n={2} title="Inspect live health" done={age !== null && age < 180} sub="telemetry the collector reads from the cluster every few seconds">
        <div className="grid cols-4">
          <Stat label="Cluster" value={cluster.name} hint={cluster.primary_host ? "observed by the collector" : "not observed"} />
          <Stat label="Tenants" value={tenants.length} hint="registered on this cluster" />
          <Stat label="Objectives met" value={slo.data ? `${metCount} of ${slo.data.length}` : "–"} hint="last 15 minutes" />
          <Stat label="Last collector snapshot" value={age === null ? "none" : age < 90 ? `${age.toFixed(0)} s ago` : `${(age / 60).toFixed(0)} min ago`} hint={age === null ? "the collector has written nothing recently" : undefined} />
        </div>
        <p className="secondary" style={{ marginTop: 10 }}>Charts: <Link to="/app/overview">Overview</Link> · <Link to="/app/workloads">Workloads</Link> · <Link to="/app/health">Database Health</Link>.</p>
      </Step>

      <Step n={3} title="A tenant with a measurable problem" done={!!q} sub={`when the problem occurs, and where DBPilot comes into use`}>
        {!target ? <div className="empty">No tenant is registered.</div> : (
          <>
            <p className="secondary">
              <strong>{target.db_role}</strong> runs analytical queries. Its objective is {targetSlo ? <>p{targetSlo.percentile} under <strong>{fmt.ms(targetSlo.threshold_ms)}</strong>; observed <strong>{fmt.ms(targetSlo.observed_ms)}</strong> over {targetSlo.windows} windows</> : "not set"}.
              {" "}{violating ? <Badge tone="critical">violating</Badge> : <Badge tone="good">meeting</Badge>}
            </p>
            {!violating && operator && targetSlo && (
              <p className="secondary">
                Its objective is loose enough that the problem below does not breach it. A customer's objective is configuration: tightening it to <strong>{fmt.ms(tight)}</strong> is how this demo makes the problem count.
                {" "}<button disabled={busy === "objective"} onClick={() => setObjective(tight)}>Set objective to p{targetSlo.percentile} ≤ {tight} ms</button>
              </p>
            )}
            {violating && operator && targetSlo && targetSlo.threshold_ms < RESTORED_OBJECTIVE_MS && (
              <p className="secondary">Restore the registered objective when you are done: <button disabled={busy === "objective"} onClick={() => setObjective(RESTORED_OBJECTIVE_MS)}>Restore p{targetSlo.percentile} ≤ {fmt.ms(RESTORED_OBJECTIVE_MS)}</button></p>
            )}
            {!violating && operator && targetSlo && targetSlo.threshold_ms < RESTORED_OBJECTIVE_MS && (
              <p className="secondary">The demo objective is in force and is being met. <button disabled={busy === "objective"} onClick={() => setObjective(RESTORED_OBJECTIVE_MS)}>Restore p{targetSlo.percentile} ≤ {fmt.ms(RESTORED_OBJECTIVE_MS)}</button></p>
            )}
            <h3>Where its time goes</h3>
            <Load of={topQuery} empty="No statements from this tenant in the last 15 minutes. Is the demo workload running?">{(rows) => rows.length === 0 ? <div className="empty">No statements from this tenant in the last 15 minutes. Is the demo workload running?</div> : (
              <>
                <pre className="trace-result" style={{ maxHeight: 120 }}>{rows[0].query}</pre>
                <p className="secondary">
                  {fmt.n(rows[0].calls)} calls in 15 minutes, <strong>{fmt.ms(rows[0].mean_exec_ms)}</strong> each, {fmt.pct(rows[0].time_share, 1)} of this tenant's database time, {fmt.n(rows[0].shared_blks_read)} blocks read from disk.{" "}
                  <button disabled={busy === "plan"} onClick={() => showPlan(rows[0].queryid)}>{plan?.queryid === rows[0].queryid ? "Plan shown" : "Show the planner's plan"}</button>
                </p>
                {plan && plan.queryid === rows[0].queryid && (
                  <>
                    <pre className="trace-result" style={{ maxHeight: 220 }}>{plan.text}</pre>
                    {/Seq Scan/.test(plan.text) && <p className="notice">The plan contains a sequential scan: each call reads a whole partition to find a few rows. That is the problem. It is not fixed by watching it; it needs a change to the database, which is what the next steps propose, test and apply.</p>}
                  </>
                )}
              </>
            )}</Load>
          </>
        )}
      </Step>

      <Step n={4} title="Ask a proposer" done={list.length > 0} sub="the rule-based proposer is always available; the LLM proposer needs a model API key on the server">
        <div className="row">
          <label className="row"><span className="muted">Verification</span>
            <select value={mode} onChange={(e) => setMode(e.target.value as "full" | "canary_only")} aria-label="Verification mode">
              <option value="full">full: static rules, planner, digital twin, canary (about 17 min on this laptop)</option>
              <option value="canary_only">canary only: skip the twin (fast; for rehearsing the canary)</option>
            </select></label>
        </div>
        {operator ? (
          <div className="row" style={{ marginTop: 12 }}>
            <button className="primary" disabled={busy !== null} onClick={preview}>{busy === "preview" ? "Evaluating the rules…" : "Run the rule-based proposer"}</button>
            <button disabled={busy !== null || !agent.data?.configured} onClick={askAgent} title={agent.data?.configured ? "" : "no model API key is configured on the server"}>{busy === "agent" ? "The LLM agent is investigating…" : "Run the LLM agent"}</button>
            <span className="muted">{agent.data == null ? "" : agent.data.configured ? `LLM provider: ${agent.data.provider}, model ${agent.data.model}. A run takes minutes on a free tier.` : "No LLM provider key is configured on this server, so the LLM proposer is unavailable. The rule-based proposer is real and needs none."}</span>
          </div>
        ) : <p className="notice">Proposing needs the operator role.</p>}
        {candidates && candidates.length > 0 && (
          <>
            <h3 style={{ marginTop: 14 }}>What the rules found</h3>
            <p className="secondary">Nothing is queued yet. Queue the one to follow; each queued proposal is verified one after another.</p>
            <div className="table-wrap"><table>
              <thead><tr><th>Candidate action</th><th>Why the rules propose it</th><th /></tr></thead>
              <tbody>{candidates.map((c, i) => (
                <tr key={i}>
                  <td><strong>{describeAction(c.action)}</strong>{c.action.tenant_role === target?.db_role && <span className="muted"> · the tenant above</span>}</td>
                  <td className="secondary" style={{ maxWidth: 520 }}>{c.rationale}</td>
                  <td><button className="primary" disabled={busy !== null} onClick={() => queue(c.action)}>Queue this one</button></td>
                </tr>
              ))}</tbody>
            </table></div>
          </>
        )}
        <h3 style={{ marginTop: 14 }}>Follow this proposal</h3>
        <Load of={proposals} empty="No proposals yet. Run a proposer above.">{(rows) => rows.length === 0 ? <div className="empty">No proposals yet. Run a proposer above.</div> : (
          <select aria-label="Proposal to follow" value={chosen ?? ""} onChange={(e) => select(e.target.value)} style={{ maxWidth: "100%" }}>
            {rows.slice(0, 25).map((p) => <option key={p.id} value={p.id}>{`${fmt.datetime(p.created_at)} · ${p.source} · ${describeAction(p.action)} · ${p.state.toLowerCase().replace("_", " ")}`}</option>)}
          </select>
        )}</Load>
      </Step>

      {!d ? <Card><div className="empty">{chosen ? "Loading the proposal…" : "Steps 5 to 12 follow one proposal. Run a proposer in step 4, or choose an existing proposal."}</div></Card> : (
        <>
          <Step n={5} title="The candidate action, and why it was proposed" done>
            <p><strong>{describeAction(d.action)}</strong> <Badge>{d.source}</Badge></p>
            <p className="secondary">{d.rationale || "No rationale was recorded."}</p>
            <div className="grid cols-2">
              <div><h3>The typed action</h3><pre className="trace-result" style={{ maxHeight: 200 }}>{JSON.stringify(d.action, null, 2)}</pre></div>
              <div>
                <h3>Evidence it was proposed on</h3>
                {d.evidence?.rule ? <p className="secondary">Rule <span className="mono">{d.evidence.rule}</span>{d.evidence.planner_ratio != null && <>: the planner estimated the index would cut the query's cost to <strong>{fmt.pct(d.evidence.planner_ratio, 1)}</strong> of what it is</>}.</p>
                  : d.evidence?.trace ? <p className="secondary">An LLM agent ({d.evidence.provider ?? "anthropic"}, <span className="mono">{d.evidence.model}</span>) made {d.evidence.trace.length} tool calls before proposing this. They are listed on the <Link to={`/app/recommendations/${d.id}`}>proposal page</Link>; its rationale is a claim, and the steps below test it.</p>
                  : <p className="secondary">Proposed by hand; no machine evidence is attached.</p>}
              </div>
            </div>
          </Step>

          <Step n={6} title="Static rules, planner what-if, digital twin" done={d.steps.length > 0} sub="cheapest check first; the first tier that rejects stops the proposal">
            <Stepper stages={STAGES} at={lc!.at} tone={lc!.tone} label={lc!.label} />
            <p><strong>{lc!.meaning}</strong> <span className="secondary">{lc!.next}</span></p>
            <Steps steps={d.steps} />
            {IN_FLIGHT.includes(d.state) && <p className="muted">This page refreshes by itself every few seconds.</p>}
          </Step>

          <Step n={7} title="Control versus treatment, per tenant" done={!!twin} sub="what the digital twin measured; it measures, it does not decide">
            {twin ? <TwinRun run={twin} target={d.action.tenant_role} /> : (
              <div className="empty">{stoppedAt && stoppedAt !== "T2" ? `Not run: the ${TIER_WORDS[stoppedAt]} (${stoppedAt}) refused this proposal first, so no replay was spent on it.`
                : d.verification === "full" ? (d.state === "FAILED" ? "The digital twin produced no result: the run failed (see the reason in step 11)." : "The digital twin has not produced a result for this proposal yet.")
                : `This proposal's verification mode is "${d.verification.replace("_", " ")}", so the twin was skipped and there is no measurement. Choose "full" in step 4 to get one.`}</div>
            )}
          </Step>

          <Step n={8} title="The verdict, and what it means" done={!!verdict} sub="the safety gate decides from the evidence above">
            {verdict ? (
              <>
                <p><Badge>{verdict.decision}</Badge> <span className="secondary">gate mode: {verdict.mode.replace("_", "-")}</span></p>
                <p><strong>{verdictMeaning(verdict.decision).text}</strong></p>
                <ul className="secondary" style={{ paddingLeft: 18 }}>{verdict.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>
                {verdict.decision === "INCONCLUSIVE" && worstUncertain > (verdict.looks ?? 0) && (
                  <p className="secondary">Why it is uncertain: the interval of at least one neighbouring tenant is too wide to rule out a 5% slowdown with {verdict.looks} replay pairs. Judging from how much the pairs disagreed, showing every neighbour unharmed would take about <strong>{worstUncertain}</strong> pairs on this machine, which is why uncertainty matters here and the gate is not loosened to approve.</p>
                )}
              </>
            ) : <div className="empty">No gate verdict: {stoppedAt && stoppedAt !== "T2" ? `the proposal never reached the gate; the ${TIER_WORDS[stoppedAt]} (${stoppedAt}) rejected it.` : d.verification === "full" ? "verification has not finished." : "the twin was not used for this proposal."}</div>}
          </Step>

          <Step n={9} title="The human decision" done={!["PROPOSED", "VERIFYING", "AWAITING_APPROVAL", "INCONCLUSIVE"].includes(d.state)} sub="operators confirm a verified change; only an admin may override an inconclusive one; the reason is audited">
            <p className="secondary">State: <Badge>{d.state}</Badge> {d.state_reason}</p>
            <DecisionPanel d={d} onDone={() => { detail.reload(); proposals.reload(); }} />
            {!["AWAITING_APPROVAL", "INCONCLUSIVE", "APPLIED"].includes(d.state) && <p className="muted">Nothing is waiting for a decision on this proposal.</p>}
          </Step>

          <Step n={10} title="The canary, in production" done={!!d.canary} sub="the change is live; each tenant's p95 is compared with its baseline every collector window">
            {d.canary ? <CanaryView canary={d.canary} /> : <div className="empty">{["REJECTED", "ADVISORY", "FAILED"].includes(d.state) ? "No canary: this proposal never went to production." : "No canary yet. A proposal reaches one only after it has been verified and approved."}</div>}
          </Step>

          <Step n={11} title="The outcome" done={["APPLIED", "ROLLED_BACK", "REJECTED", "FAILED"].includes(d.state)} sub="applied, or undone with its stored inverse">
            <p><Badge>{d.state}</Badge> <strong>{lc!.label ?? d.state}</strong></p>
            <p className="secondary">{lc!.meaning} {lc!.next}</p>
            {d.state_reason && <p className="secondary"><span className="muted">Reason recorded: </span>{d.state_reason}</p>}
            {operator && appliedIndex && (
              <div className="notice">
                <strong>Optional: a harmful change.</strong> This change built <span className="mono">{appliedIndex}</span>. Proposing to drop it makes {d.action.tenant_role ?? "the tenant"} slow again, with the canary as the only check.
                The canary should see that tenant over its limit and undo the drop by itself.{" "}
                <button disabled={busy === "drop"} onClick={proposeDrop}>Propose dropping it</button>
              </div>
            )}
          </Step>

          <Step n={12} title="Audit history and recorded outcome" done={mineAudit.length > 0} sub="appended by the database; it cannot be edited or deleted">
            <Load of={audit}>{() => mineAudit.length === 0 ? <div className="empty">No audit entries for this proposal yet.</div> : (
              <div className="table-wrap"><table>
                <thead><tr><th>When</th><th>Who</th><th>What</th><th>Detail</th></tr></thead>
                <tbody>{mineAudit.map((a) => (
                  <tr key={a.id}><td className="muted">{fmt.datetime(a.created_at)}</td><td>{a.actor_email ?? <span className="muted">the engine</span>}</td><td className="mono">{a.action}</td>
                    <td className="secondary">{a.detail?.new?.state ? `${a.detail.old?.state ?? "–"} → ${a.detail.new.state}${a.detail.new.state_reason ? `: ${String(a.detail.new.state_reason).slice(0, 140)}` : ""}` : "–"}</td></tr>
                ))}</tbody>
              </table></div>
            )}</Load>
            {ledgerRow?.production_ratios && (
              <p className="secondary" style={{ marginTop: 10 }}>
                Recorded in the outcome ledger: {Object.entries<number>(ledgerRow.production_ratios).map(([k, v]) => `${k} ${fmt.ratio(v)}`).join(" · ")} against baseline in production
                {ledgerRow.twin_effects ? <>, where the twin had predicted {Object.entries<any>(ledgerRow.twin_effects).filter(([k]) => ledgerRow.production_ratios[k] != null).map(([k, e]) => `${k} ${fmt.ratio(e.ratio)}`).join(" · ")}</> : ", with no twin prediction for this proposal"}.
              </p>
            )}
            <p className="secondary">More: <Link to="/app/audit">Audit History</Link> · <Link to="/app/experiments">Experiments</Link> · <Link to={`/app/recommendations/${d.id}`}>the full proposal page</Link>.</p>
          </Step>
        </>
      )}
    </div>
  );
}
