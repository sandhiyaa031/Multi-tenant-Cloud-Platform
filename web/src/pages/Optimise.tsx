import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, can, describeAction, fmt, useApi, type Cluster, type Proposal, type ProposalDetail } from "../api";
import { NeedCluster, useAuth, useCluster } from "../App";
import { Badge, Card, EffectsPlot, KeyValue, Load, PageHead, Stat } from "../ui";

const POLL = 8000;
const IN_FLIGHT = ["PROPOSED", "VERIFYING", "APPROVED", "CANARY", "ROLLBACK_REQUESTED"];

function ProposalTable({ rows, empty }: { rows: Proposal[]; empty: string }) {
  const navigate = useNavigate();
  if (rows.length === 0) return <div className="empty">{empty}</div>;
  return (
    <div className="table-wrap"><table>
      <thead><tr><th>Action</th><th>From</th><th>Verification</th><th>State</th><th>Why it is in this state</th><th>Created</th></tr></thead>
      <tbody>{rows.map((p) => (
        <tr key={p.id} className="clickable" onClick={() => navigate(`/app/recommendations/${p.id}`)}>
          <td><strong>{describeAction(p.action)}</strong></td>
          <td>{p.source}</td>
          <td>{p.verification === "full" ? `twin + ${p.gate_mode.replace("_", "-")} gate` : p.verification.replace("_", " ")}</td>
          <td><Badge>{p.state}</Badge></td>
          <td className="secondary" style={{ maxWidth: 420 }}>{p.state_reason || "–"}</td>
          <td className="muted">{fmt.datetime(p.created_at)}</td>
        </tr>
      ))}</tbody>
    </table></div>
  );
}

// ── Agent console ────────────────────────────────────────────────────────────

export function AgentConsole() {
  return <NeedCluster>{(c) => <AgentFor cluster={c} />}</NeedCluster>;
}
function AgentFor({ cluster }: { cluster: Cluster }) {
  const { me } = useAuth();
  const operator = can(me?.org.role, "OPERATOR");
  const proposals = useApi<Proposal[]>(`/proposals?cluster_id=${cluster.id}&limit=100`, POLL);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState("");
  const [created, setCreated] = useState<Proposal[] | null>(null);
  const run = (source: "rule" | "agent") => {
    setBusy(source); setError(null); setCreated(null);
    api<Proposal[]>(`/clusters/${cluster.id}/diagnose`, { method: "POST", body: { source, hint } })
      .then((p) => { setCreated(p); proposals.reload(); })
      .catch((e: Error) => setError(e.message)).finally(() => setBusy(null));
  };
  const fromAgent = (proposals.data ?? []).filter((p) => p.source === "agent");
  const latest = fromAgent[0];
  return (
    <div className="stack">
      <PageHead title="Agent Console">
        Ask a proposer to diagnose the cluster. It reads telemetry through read-only tools and may propose one typed action.
        It cannot run SQL and cannot approve its own proposal; whatever it proposes goes to verification.
      </PageHead>
      <Card title="Run a diagnosis">
        {!operator ? <div className="notice">Running a diagnosis needs the operator role.</div> : (
          <>
            <div className="field"><label>Note to the agent (optional)</label>
              <input value={hint} onChange={(e) => setHint(e.target.value)} placeholder="e.g. the analytics tenant reports slow item lookups" style={{ width: "100%" }} /></div>
            <div className="row">
              <button className="primary" disabled={busy !== null} onClick={() => run("agent")}>{busy === "agent" ? "Agent is investigating…" : "Run LLM agent"}</button>
              <button disabled={busy !== null} onClick={() => run("rule")}>{busy === "rule" ? "Evaluating rules…" : "Run rule-based proposer"}</button>
              <span className="muted">The agent can take up to a minute. It requires an API key configured on the server.</span>
            </div>
          </>
        )}
        {error && <p className="error" style={{ marginTop: 12 }}>{error}</p>}
        {created && <p className="notice" style={{ marginTop: 12 }}>{created.length === 0 ? "No rule matched: nothing proposed." : `${created.length} proposal(s) queued for verification.`}</p>}
      </Card>
      {latest && (
        <Card title="Latest agent run" sub={<>{fmt.datetime(latest.created_at)} · model {latest.evidence.model ?? "unknown"}</>} action={<Link to={`/app/recommendations/${latest.id}`}>Open proposal</Link>}>
          <p><strong>{describeAction(latest.action)}</strong> <Badge>{latest.state}</Badge></p>
          <p className="secondary">{latest.rationale}</p>
          <Trace evidence={latest.evidence} />
        </Card>
      )}
      <Card title="Agent proposals"><Load of={proposals}>{() => <ProposalTable rows={fromAgent} empty="The agent has not proposed anything on this cluster yet." />}</Load></Card>
    </div>
  );
}

// What the agent looked at, in order: every tool call with its input and what came back.
function Trace({ evidence }: { evidence: Record<string, any> }) {
  const trace: any[] = evidence.trace ?? [];
  if (trace.length === 0) return null;
  const u = evidence.usage;
  return (
    <>
      <h3 style={{ marginTop: 16 }}>What the agent did</h3>
      <div className="timeline">
        {trace.map((t, i) => t.kind === "note" ? (
          <div className="item" key={i}><div className="secondary">{t.text}</div></div>
        ) : (
          <div className={`item ${t.error ? "critical" : t.name === "propose_action" ? "good" : "info"}`} key={i}>
            <span className="mono"><strong>{t.name}</strong>({Object.keys(t.input ?? {}).length ? JSON.stringify(t.input) : ""})</span>
            {t.error ? <div className="error" style={{ marginTop: 6 }}>{t.error}</div> : t.name !== "propose_action" && <pre className="trace-result">{t.result}</pre>}
          </div>
        ))}
      </div>
      {u && <div className="muted">{u.requests} model requests · {fmt.n(u.input_tokens)} input tokens · {fmt.n(u.output_tokens)} output tokens</div>}
    </>
  );
}

// ── Recommendations ──────────────────────────────────────────────────────────

const TEMPLATES: Record<string, object> = {
  "Index for one tenant": { type: "create_index", table: "order_line", columns: ["ol_i_id"], tenant_role: "t_analytic" },
  "Index for all tenants": { type: "create_index", table: "order_line", columns: ["ol_i_id"] },
  "Sort memory for one tenant": { type: "role_setting", tenant_role: "t_analytic", name: "work_mem", value: "65536" },
  "Parallel workers, whole instance": { type: "instance_setting", name: "max_parallel_workers_per_gather", value: "4" },
  "Concurrency cap": { type: "concurrency_cap", tenant_role: "t_bursty", max_connections: 4 },
  "Refresh statistics": { type: "analyze", table: "orders", tenant_role: "t_mixed" },
};

export function Recommendations() {
  return <NeedCluster>{(c) => <RecommendationsFor cluster={c} />}</NeedCluster>;
}
function RecommendationsFor({ cluster }: { cluster: Cluster }) {
  const { me } = useAuth();
  const proposals = useApi<Proposal[]>(`/proposals?cluster_id=${cluster.id}&limit=300`, POLL);
  const [show, setShow] = useState(false);
  const rows = proposals.data ?? [];
  const waiting = rows.filter((p) => ["AWAITING_APPROVAL", "INCONCLUSIVE"].includes(p.state));
  return (
    <div className="stack">
      <PageHead title="Recommendations" action={can(me?.org.role, "OPERATOR") && <button className="primary" onClick={() => setShow(!show)}>{show ? "Close" : "Propose an action"}</button>}>
        Every proposed change and where it stands. A proposal is only data until it passes verification; verified ones wait here for a decision.
      </PageHead>
      {show && <ProposeForm cluster={cluster} onDone={() => { setShow(false); proposals.reload(); }} />}
      <Card title="Waiting for a decision" sub="verified and awaiting approval, or inconclusive and escalated to a person">
        <Load of={proposals}>{() => <ProposalTable rows={waiting} empty="Nothing is waiting for a decision." />}</Load>
      </Card>
      <Card title="In progress"><Load of={proposals}>{() => <ProposalTable rows={rows.filter((p) => IN_FLIGHT.includes(p.state))} empty="Nothing is being verified or applied right now." />}</Load></Card>
      <Card title="History"><Load of={proposals}>{() => <ProposalTable rows={rows.filter((p) => !IN_FLIGHT.includes(p.state) && !waiting.includes(p))} empty="No completed proposals yet." />}</Load></Card>
    </div>
  );
}

function ProposeForm({ cluster, onDone }: { cluster: Cluster; onDone: () => void }) {
  const [text, setText] = useState(JSON.stringify(Object.values(TEMPLATES)[0], null, 2));
  const [rationale, setRationale] = useState("");
  const [verification, setVerification] = useState("full");
  const [gate, setGate] = useState("per_tenant");
  const [auto, setAuto] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = () => {
    let action: unknown;
    try { action = JSON.parse(text); } catch { setError("The action is not valid JSON."); return; }
    api(`/clusters/${cluster.id}/proposals`, { method: "POST", body: { action, rationale, verification, gate_mode: gate, auto_approve: auto } })
      .then(onDone).catch((e: Error) => setError(e.message));
  };
  return (
    <Card title="Propose an action" sub="must be one of the typed actions; anything else is refused by the API">
      <div className="grid cols-2">
        <div>
          <div className="field"><label>Start from</label>
            <select onChange={(e) => setText(JSON.stringify(TEMPLATES[e.target.value], null, 2))}>{Object.keys(TEMPLATES).map((k) => <option key={k}>{k}</option>)}</select></div>
          <div className="field"><label>Action</label><textarea className="mono" rows={8} value={text} onChange={(e) => setText(e.target.value)} /></div>
        </div>
        <div>
          <div className="field"><label>Rationale</label><textarea rows={3} value={rationale} onChange={(e) => setRationale(e.target.value)} /></div>
          <div className="field"><label>Verification</label>
            <select value={verification} onChange={(e) => setVerification(e.target.value)}>
              <option value="full">Full: static rules, what-if, digital twin, canary</option>
              <option value="canary_only">Canary only (skips the twin)</option>
              <option value="none">None: apply and observe (for experiments)</option>
            </select></div>
          <div className="field"><label>Gate</label>
            <select value={gate} onChange={(e) => setGate(e.target.value)}>
              <option value="per_tenant">Per tenant: every tenant must be shown unharmed</option>
              <option value="aggregate">Aggregate: judge the workload as a whole (for comparison)</option>
            </select></div>
          <label className="row"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Proceed to canary without waiting for approval if verification passes</label>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <button className="primary" onClick={submit}>Submit for verification</button>
    </Card>
  );
}

export function ProposalPage() {
  const { id } = useParams();
  const { me } = useAuth();
  const p = useApi<ProposalDetail>(`/proposals/${id}`, 5000);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const decide = (verb: string) => api(`/proposals/${id}/${verb}`, { method: "POST", body: { reason } }).then(p.reload).catch((e: Error) => setError(e.message));
  return (
    <Load of={p}>{(d) => {
      const role = me?.org.role;
      const canApprove = (d.state === "AWAITING_APPROVAL" && can(role, "OPERATOR")) || (d.state === "INCONCLUSIVE" && can(role, "ADMIN"));
      const canReject = ["AWAITING_APPROVAL", "INCONCLUSIVE"].includes(d.state) && can(role, "OPERATOR");
      const twin = d.twin_runs.at(-1);
      return (
        <div className="stack">
          <PageHead title={describeAction(d.action)} action={<Link to="/app/recommendations">← All recommendations</Link>}>
            Proposed by {d.source} · {fmt.datetime(d.created_at)}
          </PageHead>
          <Card title="Status" action={<Badge>{d.state}</Badge>}>
            <p className="secondary">{d.state_reason || "Queued for verification."}</p>
            {d.state === "INCONCLUSIVE" && <p className="notice">Verification could not show this change is safe. It will not be applied unless an admin overrides.</p>}
            {(canApprove || canReject || (d.state === "APPLIED" && can(role, "OPERATOR"))) && (
              <div className="row">
                <input placeholder="Reason (recorded in the audit log)" value={reason} onChange={(e) => setReason(e.target.value)} style={{ flex: 1, minWidth: 260 }} />
                {canApprove && <button className="primary" onClick={() => decide("approve")}>{d.state === "INCONCLUSIVE" ? "Override and approve" : "Approve for canary"}</button>}
                {canReject && <button className="danger" onClick={() => decide("reject")}>Reject</button>}
                {d.state === "APPLIED" && <button className="danger" onClick={() => decide("rollback")}>Roll back</button>}
              </div>
            )}
            {error && <p className="error" style={{ marginTop: 10 }}>{error}</p>}
          </Card>
          <div className="grid cols-2">
            <Card title="Action" sub="the typed action; the executor compiles it to SQL"><pre className="trace-result" style={{ maxHeight: 260 }}>{JSON.stringify(d.action, null, 2)}</pre></Card>
            <Card title="Rationale">
              <p className="secondary">{d.rationale || "No rationale was given."}</p>
              <KeyValue rows={[["Verification", d.verification.replace("_", " ")], ["Gate", d.gate_mode.replace("_", " ")], ["Auto-approve", d.auto_approve ? "yes" : "no"]]} />
            </Card>
          </div>
          <Card title="Verification" sub="tiers run cheapest first; the first that rejects stops the proposal"><Steps steps={d.steps} /></Card>
          {twin && <TwinRun run={twin} />}
          {d.canary && <CanaryView canary={d.canary} />}
          {d.evidence?.trace && <Card title="Agent trace"><Trace evidence={d.evidence} /></Card>}
        </div>
      );
    }}</Load>
  );
}

const TIER_NAME: Record<string, string> = { T0: "Static rules", T1: "Planner what-if", T2: "Digital twin", T3: "Canary" };
function Steps({ steps }: { steps: ProposalDetail["steps"] }) {
  if (steps.length === 0) return <div className="empty">Verification has not started yet.</div>;
  const tone = (d: string) => (d === "APPROVE" ? "good" : d === "REJECT" ? "critical" : d === "INCONCLUSIVE" ? "warning" : "");
  return (
    <div className="timeline">
      {steps.map((s, i) => (
        <div className={`item ${tone(s.decision)}`} key={i}>
          <div className="row"><strong>{s.tier} · {TIER_NAME[s.tier]}</strong><Badge>{s.decision}</Badge><span className="muted">{s.seconds >= 1 ? `${s.seconds.toFixed(0)} s` : "< 1 s"}</span></div>
          <div className="secondary">{s.summary}</div>
        </div>
      ))}
    </div>
  );
}

function TwinRun({ run }: { run: any }) {
  const v = run.verdict;
  return (
    <Card title="Digital twin result" sub={`${run.transactions} captured transactions in ${v.looks ?? 1} window(s) of ${run.window_s.toFixed(0)} s, replayed ${run.repetitions}× per arm in total`} action={<Badge>{v.decision}</Badge>}>
      <div className="grid cols-4" style={{ marginBottom: 14 }}>
        <Stat label="Replay errors" value={run.replay_errors} hint="across both arms" />
        <Stat label="Write volume" value={run.wal_ratio == null ? "–" : fmt.ratio(run.wal_ratio)} hint="WAL, treatment ÷ control" />
        <Stat label="Storage added" value={fmt.bytes(run.storage_delta_bytes)} />
        <Stat label="Time to apply" value={run.apply_seconds == null ? "–" : `${run.apply_seconds.toFixed(1)} s`} hint="on the clone" />
      </div>
      {v.mode === "per_tenant" ? (
        <>
          <h3>Effect per tenant: p95 latency, treatment ÷ control, with confidence interval</h3>
          <EffectsPlot effects={v.effects} />
          <div className="table-wrap" style={{ marginTop: 10 }}><table>
            <thead><tr><th>Tenant / class</th><th>Finding</th><th className="num">Control p95</th><th className="num">Treatment p95</th><th className="num">Ratio</th><th className="num">Interval</th><th className="num">Samples</th></tr></thead>
            <tbody>{Object.entries<any>(v.effects).sort().map(([k, e]) => (
              <tr key={k}><td>{k}</td><td><Badge>{e.status}</Badge></td><td className="num">{fmt.ms(e.control)}</td><td className="num">{fmt.ms(e.treatment)}</td>
                <td className="num">{fmt.ratio(e.ratio)}</td><td className="num">{e.lo == null ? "–" : `${e.lo.toFixed(2)} – ${e.hi.toFixed(2)}`}</td><td className="num">{e.n_control} / {e.n_treatment}</td></tr>
            ))}</tbody>
          </table></div>
        </>
      ) : <p className="notice">Judged with the aggregate gate: only the workload as a whole was compared, so per-tenant effects were not examined.</p>}
      <h3 style={{ marginTop: 14 }}>Reasons</h3>
      <ul className="secondary" style={{ margin: 0, paddingLeft: 18 }}>{v.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>
      {v.shadow && (
        <p className="secondary" style={{ marginTop: 12 }}>
          For comparison only, the {v.shadow.mode === "aggregate" ? "aggregate" : "per-tenant"} gate on the same measurements: <Badge>{v.shadow.decision}</Badge> {v.shadow.reasons.join("; ")}
        </p>
      )}
      {v.calibration && (
        <p className="secondary">
          Canary tolerance {fmt.pct(v.calibration.contract_tolerance)}, {v.calibration.history_pairs >= 8
            ? `calibrated from ${v.calibration.history_pairs} earlier twin-versus-production comparisons of this kind of action`
            : `the default (${v.calibration.history_pairs} earlier comparisons of this kind of action; 8 are needed to calibrate)`}.
        </p>
      )}
    </Card>
  );
}

function CanaryView({ canary }: { canary: any }) {
  const keys = Object.keys(canary.contract).sort();
  const staged = canary.observations.some((o: any) => (o.stage ?? 1) > 1);
  const guards = canary.observations.flatMap((o: any, i: number) => (o.breaches ?? []).filter((b: string) => !b.includes("/")).map((b: string) => `Window ${i + 1}: ${b}`));
  return (
    <Card title="Canary" sub="production latency against the contract, per collector window" action={canary.outcome ? <Badge>{canary.outcome}</Badge> : <Badge>CANARY</Badge>}>
      {canary.outcome_reason && <p className="secondary">{canary.outcome_reason}</p>}
      <div className="table-wrap"><table>
        <thead><tr><th>Tenant / class</th><th className="num">Baseline p95</th><th className="num">Contract</th>{canary.observations.map((o: any, i: number) => <th className="num" key={i} title={o.stage_label}>{staged ? `Stage ${o.stage ?? 1} · ` : ""}Window {i + 1}</th>)}<th className="num">Result</th></tr></thead>
        <tbody>{keys.map((k) => (
          <tr key={k}><td>{k}</td><td className="num">{fmt.ms(canary.baseline[k])}</td><td className="num">≤ {fmt.ratio(canary.contract[k])}</td>
            {canary.observations.map((o: any, i: number) => {
              const r = o.ratios?.[k]; const over = r != null && r > canary.contract[k] && (o.counts?.[k] ?? 0) >= 5;
              return <td className="num" key={i} style={over ? { color: "#ffb4b4", fontWeight: 600 } : undefined}>{!o.telemetry ? "no data" : r == null ? "–" : fmt.ratio(r)}</td>;
            })}
            <td className="num"><strong>{fmt.ratio(canary.result?.[k])}</strong></td></tr>
        ))}</tbody>
      </table></div>
      {guards.length > 0 && <p className="notice">{guards.join("; ")}</p>}
      <div className="grid cols-2" style={{ marginTop: 14 }}>
        <div><h3>Applied to production</h3><pre className="trace-result">{canary.applied.join("\n")}</pre></div>
        <div><h3>Inverse, used for rollback</h3><pre className="trace-result">{canary.inverse.length ? canary.inverse.join("\n") : "nothing to undo"}</pre></div>
      </div>
    </Card>
  );
}

// ── Lists that follow one stage across proposals ─────────────────────────────

function useDetails(cluster: Cluster, filter: (p: Proposal) => boolean, limit = 12) {
  const list = useApi<Proposal[]>(`/proposals?cluster_id=${cluster.id}&limit=200`, POLL);
  const ids = (list.data ?? []).filter(filter).slice(0, limit).map((p) => p.id);
  return { list, ids };
}
function Detail({ id, children }: { id: string; children: (d: ProposalDetail) => React.ReactNode }) {
  const d = useApi<ProposalDetail>(`/proposals/${id}`, POLL);
  return d.data ? <>{children(d.data)}</> : null;
}
function Heading({ d }: { d: ProposalDetail }) {
  return <div className="spread" style={{ marginBottom: 8 }}><Link to={`/app/recommendations/${d.id}`}><strong>{describeAction(d.action)}</strong></Link><span className="row"><span className="muted">{fmt.datetime(d.created_at)}</span><Badge>{d.state}</Badge></span></div>;
}

export function TwinLab() {
  return <NeedCluster>{(c) => <TwinFor cluster={c} />}</NeedCluster>;
}
function TwinFor({ cluster }: { cluster: Cluster }) {
  const twin = useApi<any>(`/clusters/${cluster.id}/twin`, 5000);
  const { list, ids } = useDetails(cluster, (p) => p.verification === "full" && !["PROPOSED", "ADVISORY"].includes(p.state), 6);
  return (
    <div className="stack">
      <PageHead title="Digital Twin Lab">
        A standby that deliberately trails production is cloned into a control and a treatment database. The proposed action is applied to
        treatment only, the captured production workload is replayed against both, and the difference is measured per tenant.
      </PageHead>
      <Card title="Experimentation plane" sub="live state of the twin node">
        <Load of={twin}>{(t) => !t.available ? <div className="notice">The twin node is not reachable ({t.error}).</div> : (
          <div className="grid cols-4">
            <Stat label="Twin node" value={t.busy ? "replaying" : "idle"} hint={t.busy ? "a run is in progress" : "ready for a run"} />
            <Stat label="Source trails production by" value={t.source.replay_timestamp ? `${Math.max(0, (Date.now() - new Date(t.source.replay_timestamp).getTime()) / 1000).toFixed(0)} s` : "–"} hint={`configured delay ${t.source.configured_delay_s} s`} />
            <Stat label="Replayed up to" value={<span className="mono" style={{ fontSize: 18 }}>{t.source.replay_lsn}</span>} hint="WAL position applied" />
            <Stat label="Received up to" value={<span className="mono" style={{ fontSize: 18 }}>{t.source.receive_lsn}</span>} hint="WAL held, not yet applied" />
          </div>
        )}</Load>
      </Card>
      <Load of={list}>{() => ids.length === 0 ? <div className="empty">No twin runs yet. Submit a recommendation with full verification to see one here.</div> : (
        <>{ids.map((id) => <Detail key={id} id={id}>{(d) => d.twin_runs.length ? <div><Heading d={d} /><TwinRun run={d.twin_runs.at(-1)} /></div> : null}</Detail>)}</>
      )}</Load>
    </div>
  );
}

export function Verification() {
  return <NeedCluster>{(c) => <VerificationFor cluster={c} />}</NeedCluster>;
}
function VerificationFor({ cluster }: { cluster: Cluster }) {
  const { list, ids } = useDetails(cluster, (p) => p.state !== "PROPOSED", 20);
  return (
    <div className="stack">
      <PageHead title="Verification">Each proposal's path through the four tiers, and the tier that decided it.</PageHead>
      <Load of={list}>{() => ids.length === 0 ? <div className="empty">Nothing has been verified yet.</div> : (
        <>{ids.map((id) => <Detail key={id} id={id}>{(d) => <Card><Heading d={d} /><Steps steps={d.steps} /></Card>}</Detail>)}</>
      )}</Load>
    </div>
  );
}

export function Canaries() {
  return <NeedCluster>{(c) => <CanariesFor cluster={c} />}</NeedCluster>;
}
function CanariesFor({ cluster }: { cluster: Cluster }) {
  const { list, ids } = useDetails(cluster, (p) => ["CANARY", "APPLIED", "ROLLED_BACK", "ROLLBACK_REQUESTED"].includes(p.state), 10);
  return (
    <div className="stack">
      <PageHead title="Canary Deployments">
        Changes applied to production under watch. Each tenant's p95 is compared with its baseline every collector window; a breach in two
        of three windows, or lost telemetry, runs the stored inverse automatically.
      </PageHead>
      <Load of={list}>{() => ids.length === 0 ? <div className="empty">No change has reached production yet.</div> : (
        <>{ids.map((id) => <Detail key={id} id={id}>{(d) => d.canary ? <div><Heading d={d} /><CanaryView canary={d.canary} /></div> : null}</Detail>)}</>
      )}</Load>
    </div>
  );
}

export function Experiments() {
  return <NeedCluster>{(c) => <ExperimentsFor cluster={c} />}</NeedCluster>;
}
function ExperimentsFor({ cluster }: { cluster: Cluster }) {
  const { tenants } = useCluster();
  const summary = useApi<any>(`/clusters/${cluster.id}/experiments`, 15000);
  const ledger = useApi<any[]>(`/clusters/${cluster.id}/ledger?limit=200`, 15000);
  const label = (g: any) => (g.verification === "full" ? `twin + ${g.gate_mode.replace("_", "-")} gate + canary` : g.verification === "canary_only" ? "canary only" : "no verification");
  return (
    <div className="stack">
      <PageHead title="Experiments">
        Computed from the outcome ledger of this cluster: for each way of producing and verifying proposals, how many reached production,
        how many harmed a tenant there, and how often the twin predicted the direction production then showed. Nothing here is estimated.
      </PageHead>
      <Card title="By proposer and verification">
        <Load of={summary}>{(s) => s.groups.length === 0 ? <div className="empty">No proposals yet, so there is nothing to compare.</div> : (
          <>
            <div className="table-wrap"><table>
              <thead><tr><th>Proposer</th><th>Verification</th><th className="num">Proposals</th><th className="num">Reached production</th><th className="num">Harmed a tenant there</th><th className="num">Rolled back</th><th className="num">Twin direction agreed</th><th>Outcomes</th></tr></thead>
              <tbody>{s.groups.map((g: any, i: number) => (
                <tr key={i}><td>{g.source}</td><td>{label(g)}</td><td className="num">{g.proposals}</td><td className="num">{g.reached_production}</td>
                  <td className="num">{g.reached_production ? `${g.harmful_in_production} (${fmt.pct(g.harmful_in_production / g.reached_production)})` : "–"}</td>
                  <td className="num">{g.rolled_back}</td>
                  <td className="num">{g.direction_checks ? `${g.direction_agreements} / ${g.direction_checks}` : "–"}</td>
                  <td className="secondary">{Object.entries<number>(g.states).map(([k, v]) => `${v} ${k.toLowerCase().replace("_", " ")}`).join(", ")}</td></tr>
              ))}</tbody>
            </table></div>
            <p className="muted" style={{ marginTop: 10 }}>
              "Harmed" means a tenant other than the target ran more than {fmt.pct(s.harm_ratio - 1)} slower at p95 during the canary windows than before the change.
              Direction agreement compares the twin's ratio with production's for the same tenant and class, treating changes within ±{fmt.pct(s.deadband)} as no change.
            </p>
          </>
        )}</Load>
      </Card>
      <Card title="Prediction against outcome" sub="every proposal that has both a twin measurement and a production measurement">
        <Load of={ledger}>{(rows) => {
          const both = rows.filter((r) => r.twin_effects && r.production_ratios);
          if (both.length === 0) return <div className="empty">No proposal has been measured on both the twin and production yet.</div>;
          const role = Object.fromEntries(tenants.map((t) => [t.id, t.db_role]));
          return (
            <div className="table-wrap"><table>
              <thead><tr><th>Action</th><th>Tenant / class</th><th className="num">Twin predicted</th><th className="num">Production showed</th><th>Outcome</th></tr></thead>
              <tbody>{both.flatMap((r) => Object.keys(r.production_ratios).sort().filter((k) => r.twin_effects[k]?.ratio != null).map((k) => (
                <tr key={r.proposal_id + k}><td><Link to={`/app/recommendations/${r.proposal_id}`}>{describeAction(r.action)}</Link></td>
                  <td>{k}{role[r.target_tenant_id] && k.startsWith(role[r.target_tenant_id] + "/") ? <span className="muted"> · target</span> : ""}</td>
                  <td className="num">{fmt.ratio(r.twin_effects[k].ratio)}</td><td className="num">{fmt.ratio(r.production_ratios[k])}</td><td><Badge>{r.state}</Badge></td></tr>
              )))}</tbody>
            </table></div>
          );
        }}</Load>
      </Card>
    </div>
  );
}
