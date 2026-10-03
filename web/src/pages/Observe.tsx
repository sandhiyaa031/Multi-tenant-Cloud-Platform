import { useState } from "react";
import { Link } from "react-router-dom";
import { api, can, fmt, useApi, type Cluster, type Proposal } from "../api";
import { NeedCluster, useAuth, useCluster } from "../App";
import { Badge, Card, Load, PageHead, RANGES, Segmented, Stat, Swatch, TimeSeries, pivot } from "../ui";

const POLL = 15000;

// The stages of the lifecycle, with how many proposals are in each right now.
function Lifecycle({ proposals }: { proposals: Proposal[] }) {
  const count = (...states: string[]) => proposals.filter((p) => states.includes(p.state)).length;
  const stages: [string, number][] = [
    ["Proposed", count("PROPOSED")], ["Verifying", count("VERIFYING")], ["Awaiting approval", count("AWAITING_APPROVAL", "INCONCLUSIVE")],
    ["Canary", count("APPROVED", "CANARY")], ["Applied", count("APPLIED")], ["Rejected", count("REJECTED")], ["Rolled back", count("ROLLED_BACK")],
  ];
  return <div className="lifecycle">{stages.map(([t, n]) => <div className="stage" key={t}><div className="n">{n}</div><div className="t">{t}</div></div>)}</div>;
}

function SloTable({ rows, colors }: { rows: any[]; colors: Record<string, string> }) {
  return (
    <div className="table-wrap"><table>
      <thead><tr><th>Tenant</th><th>Class</th><th>Objective</th><th className="num">Observed</th><th className="num">Windows over</th><th>Status</th></tr></thead>
      <tbody>{rows.map((s) => {
        const none = s.windows === 0;
        const over = !none && s.observed_ms > s.threshold_ms;
        return (
          <tr key={s.tenant_role + s.query_class + s.percentile}>
            <td><Swatch color={colors[s.tenant_role]} />{s.tenant_role}</td>
            <td>{s.query_class}</td>
            <td>p{s.percentile} ≤ {fmt.ms(s.threshold_ms)}</td>
            <td className="num">{none ? "–" : fmt.ms(s.observed_ms)}</td>
            <td className="num">{none ? "–" : `${s.windows_violating} / ${s.windows}`}</td>
            <td>{none ? <Badge tone="">no traffic</Badge> : over ? <Badge tone="critical">violating</Badge> : <Badge tone="good">meeting</Badge>}</td>
          </tr>
        );
      })}</tbody>
    </table></div>
  );
}

export function Overview() {
  return <NeedCluster>{(c) => <OverviewFor cluster={c} />}</NeedCluster>;
}
function OverviewFor({ cluster }: { cluster: Cluster }) {
  const { tenants, colors } = useCluster();
  const slo = useApi<any[]>(`/clusters/${cluster.id}/slo-status?minutes=15`, POLL);
  const latency = useApi<any[]>(`/clusters/${cluster.id}/latency?minutes=30`, POLL);
  const proposals = useApi<Proposal[]>(`/proposals?cluster_id=${cluster.id}&limit=200`, POLL);
  const instance = useApi<any[]>(`/clusters/${cluster.id}/instance?minutes=15`, POLL);
  const last = instance.data?.at(-1);
  const violating = slo.data?.filter((s) => s.windows > 0 && s.observed_ms > s.threshold_ms).length;
  const oltp = (latency.data ?? []).filter((r) => r.query_class === "OLTP");
  return (
    <div className="stack">
      <PageHead title="Overview">What the cluster is doing now, who is meeting their objectives, and where each proposed change stands.</PageHead>
      {!cluster.primary_host && <div className="notice">This cluster has no primary address registered, so the collector is not observing it.</div>}
      <div className="grid cols-4">
        <Stat label="Tenants" value={tenants.length} hint={`on cluster ${cluster.name}`} />
        <Stat label="Objectives violated" value={violating ?? "–"} hint={slo.data ? `of ${slo.data.length} objectives, last 15 min` : undefined} />
        <Stat label="Transactions / s" value={last ? fmt.n(last.xact_commit / last.window_seconds) : "–"} hint="last collector window" />
        <Stat label="Replica lag" value={last ? (last.replica_lag_bytes == null ? "no replica" : fmt.bytes(last.replica_lag_bytes)) : "–"} hint="bytes of WAL not yet replayed" />
      </div>
      <Card title="Optimisation lifecycle" sub="proposals by stage" action={<Link to="/app/recommendations">All recommendations</Link>}>
        <Load of={proposals}>{(p) => <Lifecycle proposals={p} />}</Load>
      </Card>
      <div className="grid cols-2">
        <Card title="Service-level objectives" sub="last 15 minutes"><Load of={slo} empty="No objectives are defined. Add them under Tenants.">{(rows) => <SloTable rows={rows} colors={colors} />}</Load></Card>
        <Card title="OLTP latency, p95" sub="server-side, per tenant">
          <Load of={latency}>{() => <TimeSeries rows={pivot(oltp, (r) => r.window_end, (r) => r.tenant, (r) => r.p95_ms)}
            series={[...new Set(oltp.map((r) => r.tenant))].sort()} colors={nameColors(tenants, colors)} format={fmt.ms} />}</Load>
        </Card>
      </div>
    </div>
  );
}

// Telemetry endpoints label series by tenant name; colours are keyed by role.
function nameColors(tenants: { name: string; db_role: string }[], colors: Record<string, string>) {
  return Object.fromEntries(tenants.map((t) => [t.name, colors[t.db_role]]));
}

export function Tenants() {
  return <NeedCluster>{(c) => <TenantsFor cluster={c} />}</NeedCluster>;
}
function TenantsFor({ cluster }: { cluster: Cluster }) {
  const { me } = useAuth();
  const { tenants, colors, reloadTenants } = useCluster();
  const slo = useApi<any[]>(`/clusters/${cluster.id}/slo-status?minutes=15`, POLL);
  const [open, setOpen] = useState<string | null>(null);
  const operator = can(me?.org.role, "OPERATOR");
  return (
    <div className="stack">
      <PageHead title="Tenants">Each tenant shares the cluster's tables, connects as its own database role, and owns a range of warehouses and its own partitions.</PageHead>
      <Card title="Tenants on this cluster">
        {tenants.length === 0 ? <div className="empty">No tenants are registered on this cluster.</div> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Tenant</th><th>Database role</th><th>Profile</th><th>Warehouses</th><th>Objectives</th><th /></tr></thead>
            <tbody>{tenants.map((t) => {
              const mine = (slo.data ?? []).filter((s) => s.tenant_role === t.db_role);
              const bad = mine.some((s) => s.windows > 0 && s.observed_ms > s.threshold_ms);
              return (
                <tr key={t.id}>
                  <td><Swatch color={colors[t.db_role]} /><strong>{t.name}</strong></td>
                  <td className="mono">{t.db_role}</td>
                  <td>{t.profile.replace("_", " ").toLowerCase()}</td>
                  <td>{t.warehouse_lo}–{t.warehouse_hi}</td>
                  <td>{mine.length === 0 ? <span className="muted">none</span> : bad ? <Badge tone="critical">violating</Badge> : <Badge tone="good">meeting</Badge>}</td>
                  <td>{operator && <button className="link" onClick={() => setOpen(open === t.id ? null : t.id)}>{open === t.id ? "Close" : "Set objective"}</button>}</td>
                </tr>
              );
            })}</tbody>
          </table></div>
        )}
        {open && <SloForm tenantId={open} onDone={() => { setOpen(null); slo.reload(); reloadTenants(); }} />}
      </Card>
      <Card title="Objectives" sub="observed over the last 15 minutes"><Load of={slo} empty="No objectives defined yet.">{(rows) => <SloTable rows={rows} colors={colors} />}</Load></Card>
    </div>
  );
}

function SloForm({ tenantId, onDone }: { tenantId: string; onDone: () => void }) {
  const [form, setForm] = useState({ query_class: "OLTP", percentile: 99, threshold_ms: 100 });
  const [error, setError] = useState<string | null>(null);
  const save = () => api(`/tenants/${tenantId}/slos`, { method: "PUT", body: form }).then(onDone).catch((e: Error) => setError(e.message));
  return (
    <div className="row" style={{ marginTop: 14 }}>
      <select value={form.query_class} onChange={(e) => setForm({ ...form, query_class: e.target.value })}><option>OLTP</option><option>OLAP</option></select>
      <select value={form.percentile} onChange={(e) => setForm({ ...form, percentile: Number(e.target.value) })}>{[50, 95, 99].map((p) => <option key={p} value={p}>p{p}</option>)}</select>
      <span className="muted">under</span>
      <input type="number" min={1} style={{ width: 110 }} value={form.threshold_ms} onChange={(e) => setForm({ ...form, threshold_ms: Number(e.target.value) })} />
      <span className="muted">ms</span>
      <button className="primary" onClick={save}>Save objective</button>
      {error && <span className="error">{error}</span>}
    </div>
  );
}

export function Workloads() {
  return <NeedCluster>{(c) => <WorkloadsFor cluster={c} />}</NeedCluster>;
}
function WorkloadsFor({ cluster }: { cluster: Cluster }) {
  const { tenants, colors } = useCluster();
  const [minutes, setMinutes] = useState(60);
  const load = useApi<any[]>(`/clusters/${cluster.id}/tenant-load?minutes=${minutes}`, POLL);
  const latency = useApi<any[]>(`/clusters/${cluster.id}/latency?minutes=${minutes}`, POLL);
  const byName = nameColors(tenants, colors);
  const names = (rows: any[]) => [...new Set(rows.map((r) => r.tenant))].sort();
  const cls = (c: string) => (latency.data ?? []).filter((r) => r.query_class === c);
  return (
    <div className="stack">
      <PageHead title="Workloads" action={<Segmented value={minutes} options={RANGES} onChange={setMinutes} />}>
        How much each tenant is asking of the database and how long its transactions take. Bursts and shifts show here first.
      </PageHead>
      <div className="grid cols-2">
        <Card title="Statements per second" sub="per tenant">
          <Load of={load}>{(rows) => <TimeSeries rows={pivot(rows, (r) => r.window_end, (r) => r.tenant, (r) => r.calls / r.window_seconds)} series={names(rows)} colors={byName} format={(v) => fmt.n(v)} />}</Load>
        </Card>
        <Card title="Database time used" sub="execution milliseconds per second of wall time">
          <Load of={load}>{(rows) => <TimeSeries rows={pivot(rows, (r) => r.window_end, (r) => r.tenant, (r) => r.total_exec_ms / r.window_seconds)} series={names(rows)} colors={byName} format={(v) => fmt.n(v)} />}</Load>
        </Card>
        <Card title="OLTP latency, p95" sub="transactions, server-side">
          <Load of={latency}>{() => <TimeSeries rows={pivot(cls("OLTP"), (r) => r.window_end, (r) => r.tenant, (r) => r.p95_ms)} series={names(cls("OLTP"))} colors={byName} format={fmt.ms} />}</Load>
        </Card>
        <Card title="OLAP latency, p95" sub="analytical queries, server-side">
          <Load of={latency}>{() => <TimeSeries rows={pivot(cls("OLAP"), (r) => r.window_end, (r) => r.tenant, (r) => r.p95_ms)} series={names(cls("OLAP"))} colors={byName} format={fmt.ms} />}</Load>
        </Card>
      </div>
      <Card title="Write volume" sub="WAL bytes per second, per tenant">
        <Load of={load}>{(rows) => <TimeSeries rows={pivot(rows, (r) => r.window_end, (r) => r.tenant, (r) => Number(r.wal_bytes) / r.window_seconds)} series={names(rows)} colors={byName} format={fmt.bytes} />}</Load>
      </Card>
    </div>
  );
}

export function Queries() {
  return <NeedCluster>{(c) => <QueriesFor cluster={c} />}</NeedCluster>;
}
function QueriesFor({ cluster }: { cluster: Cluster }) {
  const { tenants, colors } = useCluster();
  const [minutes, setMinutes] = useState(15);
  const [tenant, setTenant] = useState("");
  const [selected, setSelected] = useState<any | null>(null);
  const top = useApi<any[]>(`/clusters/${cluster.id}/top-queries?minutes=${minutes}&limit=40${tenant ? `&tenant_id=${tenant}` : ""}`, POLL);
  const plan = useApi<any>(selected ? `/clusters/${cluster.id}/queries/${selected.queryid}/explain` : null);
  const roleOf = Object.fromEntries(tenants.map((t) => [t.id, t.db_role]));
  return (
    <div className="stack">
      <PageHead title="Query Intelligence" action={
        <div className="row">
          <select value={tenant} onChange={(e) => setTenant(e.target.value)}><option value="">All tenants</option>{tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
          <Segmented value={minutes} options={RANGES} onChange={setMinutes} />
        </div>}>
        The statements that cost the most database time, attributed to the tenant that ran them. Literals are replaced by placeholders; no row data is shown.
      </PageHead>
      <Card title="Most expensive queries" sub="by total execution time in the window; select one to see its plan">
        <Load of={top} empty="No query activity recorded in this window.">{(rows) => (
          <div className="table-wrap"><table>
            <thead><tr><th>Tenant</th><th>Query</th><th className="num">Calls</th><th className="num">Mean</th><th className="num">Share of time</th><th className="num">Disk blocks</th><th className="num">Temp blocks</th><th className="num">WAL</th></tr></thead>
            <tbody>{rows.map((q) => (
              <tr key={q.tenant_id + q.queryid} className="clickable" onClick={() => setSelected(q)}>
                <td><Swatch color={colors[roleOf[q.tenant_id]]} />{q.tenant}</td>
                <td><div className="sql" title={q.query}>{q.query}</div></td>
                <td className="num">{fmt.n(q.calls)}</td><td className="num">{fmt.ms(q.mean_exec_ms)}</td>
                <td className="num">{fmt.pct(q.time_share, 1)}</td><td className="num">{fmt.n(q.shared_blks_read)}</td>
                <td className="num">{fmt.n(q.temp_blks_written)}</td><td className="num">{fmt.bytes(Number(q.wal_bytes))}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}</Load>
      </Card>
      {selected && (
        <Card title="Execution plan" sub={`fingerprint ${selected.queryid} · planned on the twin source, not on production`} action={<button onClick={() => setSelected(null)}>Close</button>}>
          <pre className="secondary" style={{ marginBottom: 12 }}>{selected.query}</pre>
          <Load of={plan}>{(p) => p.explained ? <pre className="trace-result" style={{ maxHeight: 420 }}>{p.plan}</pre> : <div className="notice">No plan available: {p.error}</div>}</Load>
        </Card>
      )}
    </div>
  );
}

export function Health() {
  return <NeedCluster>{(c) => <HealthFor cluster={c} />}</NeedCluster>;
}
function HealthFor({ cluster }: { cluster: Cluster }) {
  const [minutes, setMinutes] = useState(60);
  const inst = useApi<any[]>(`/clusters/${cluster.id}/instance?minutes=${minutes}`, POLL);
  const settings = useApi<any>(`/clusters/${cluster.id}/settings`);
  const one = { value: "var(--series-1)" };
  const series = (f: (r: any) => number) => pivot(inst.data ?? [], (r) => r.window_end, () => "value", f);
  const last = inst.data?.at(-1);
  return (
    <div className="stack">
      <PageHead title="Database Health" action={<Segmented value={minutes} options={RANGES} onChange={setMinutes} />}>
        Instance-wide counters from PostgreSQL's statistics views, as deltas per collector window.
      </PageHead>
      <div className="grid cols-4">
        <Stat label="Database size" value={last ? fmt.bytes(last.database_bytes) : "–"} />
        <Stat label="Active connections" value={last?.active_connections ?? "–"} hint="at the last sample" />
        <Stat label="Buffer cache hit ratio" value={last && last.blks_hit + last.blks_read > 0 ? fmt.pct(last.blks_hit / (last.blks_hit + last.blks_read), 1) : "–"} hint="last window" />
        <Stat label="Deadlocks" value={inst.data ? inst.data.reduce((a, r) => a + r.deadlocks, 0) : "–"} hint={`in the last ${minutes} min`} />
      </div>
      <div className="grid cols-2">
        <Card title="Commits per second"><Load of={inst}>{() => <TimeSeries rows={series((r) => r.xact_commit / r.window_seconds)} series={["value"]} colors={one} format={(v) => fmt.n(v)} />}</Load></Card>
        <Card title="WAL generated per second"><Load of={inst}>{() => <TimeSeries rows={series((r) => Number(r.wal_bytes) / r.window_seconds)} series={["value"]} colors={one} format={fmt.bytes} />}</Load></Card>
        <Card title="Blocks read from disk per second"><Load of={inst}>{() => <TimeSeries rows={series((r) => r.blks_read / r.window_seconds)} series={["value"]} colors={one} format={(v) => fmt.n(v)} />}</Load></Card>
        <Card title="Replica lag" sub="WAL not yet replayed on the replica"><Load of={inst}>{() => <TimeSeries rows={series((r) => r.replica_lag_bytes ?? 0)} series={["value"]} colors={one} format={fmt.bytes} />}</Load></Card>
      </div>
      <Card title="Tunable settings" sub="current values of everything an action is allowed to change">
        <Load of={settings}>{(s) => (
          <div className="grid cols-2">
            <div><h3>Instance</h3><table><tbody>{Object.entries(s.instance).map(([k, v]) => <tr key={k}><td className="mono">{k}</td><td className="num mono">{String(v)}</td></tr>)}</tbody></table></div>
            <div><h3>Per-tenant overrides</h3><table><tbody>{Object.entries<any>(s.tenants).map(([role, v]) => (
              <tr key={role}><td className="mono">{role}</td><td className="mono">{v.overrides.length ? v.overrides.join(", ") : <span className="muted">none</span>}</td>
                <td className="num">{v.connection_limit === -1 ? <span className="muted">no cap</span> : `cap ${v.connection_limit}`}</td></tr>
            ))}</tbody></table></div>
          </div>
        )}</Load>
      </Card>
    </div>
  );
}
