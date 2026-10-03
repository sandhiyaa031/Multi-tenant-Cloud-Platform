import { Fragment, useState } from "react";
import { api, can, fmt, useApi, type Role } from "../api";
import { useAuth, useCluster } from "../App";
import { Badge, Card, KeyValue, Load, PageHead } from "../ui";

export function Audit() {
  const [before, setBefore] = useState<number[]>([]);
  const cursor = before.at(-1);
  const log = useApi<any[]>(`/audit?limit=50${cursor ? `&before_id=${cursor}` : ""}`);
  const [open, setOpen] = useState<number | null>(null);
  return (
    <div className="stack">
      <PageHead title="Audit History">
        Every change to this organization's resources and every decision on a proposal. Written by database triggers; entries cannot be edited or deleted.
      </PageHead>
      <Card>
        <Load of={log} empty="No audit entries.">{(rows) => (
          <>
            <div className="table-wrap"><table>
              <thead><tr><th>When</th><th>Who</th><th>What</th><th>On</th><th /></tr></thead>
              <tbody>{rows.map((e) => (
                <Fragment key={e.id}>
                  <tr className="clickable" onClick={() => setOpen(open === e.id ? null : e.id)}>
                    <td className="muted">{fmt.datetime(e.created_at)}</td>
                    <td>{e.actor_email ?? <span className="muted">system</span>}</td>
                    <td className="mono">{e.action}</td>
                    <td className="muted mono">{e.entity_id?.slice(0, 8)}</td>
                    <td>{e.action === "proposals.update" && e.detail?.new?.state ? <Badge>{e.detail.new.state}</Badge> : null}</td>
                  </tr>
                  {open === e.id && <tr><td colSpan={5}><pre className="trace-result" style={{ maxHeight: 300 }}>{JSON.stringify(e.detail, null, 2)}</pre></td></tr>}
                </Fragment>
              ))}</tbody>
            </table></div>
            <div className="row" style={{ marginTop: 12 }}>
              <button disabled={before.length === 0} onClick={() => setBefore(before.slice(0, -1))}>Newer</button>
              <button disabled={rows.length < 50} onClick={() => setBefore([...before, rows.at(-1).id])}>Older</button>
            </div>
          </>
        )}</Load>
      </Card>
    </div>
  );
}

export function Settings() {
  const { me } = useAuth();
  const { clusters } = useCluster();
  const members = useApi<any[]>("/members");
  const admin = can(me?.org.role, "ADMIN");
  const [invite, setInvite] = useState({ email: "", full_name: "", role: "VIEWER" as Role });
  const [cluster, setCluster] = useState({ name: "", pooler_host: "", pooler_port: 6432, database_name: "app", primary_host: "", primary_port: 5432 });
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const act = (p: Promise<unknown>, ok: string) => p.then(() => { setMessage({ ok: true, text: ok }); members.reload(); }).catch((e: Error) => setMessage({ ok: false, text: e.message }));
  return (
    <div className="stack">
      <PageHead title="Settings">Your organization, its members and their roles, and the clusters it manages.</PageHead>
      {message && <div className={message.ok ? "notice" : "error"}>{message.text}</div>}
      <Card title="Organization">
        <KeyValue rows={[["Name", me?.org.name], ["Identifier", <span className="mono">{me?.org.slug}</span>], ["You are signed in as", `${me?.full_name} (${me?.email})`], ["Your role", me?.org.role.toLowerCase()]]} />
      </Card>
      <Card title="Members" sub="viewers read; operators propose and decide on verified proposals; admins manage members and clusters and may override an inconclusive verification">
        <Load of={members}>{(rows) => (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Joined</th><th /></tr></thead>
            <tbody>{rows.map((m) => (
              <tr key={m.user_id}>
                <td>{m.full_name}{m.pending && <span className="muted"> · invited</span>}</td><td>{m.email}</td>
                <td>{admin ? (
                  <select value={m.role} onChange={(e) => act(api(`/members/${m.user_id}`, { method: "PATCH", body: { role: e.target.value } }), "Role updated.")}>
                    {["VIEWER", "OPERATOR", "ADMIN"].map((r) => <option key={r} value={r}>{r.toLowerCase()}</option>)}
                  </select>) : m.role.toLowerCase()}</td>
                <td className="muted">{fmt.datetime(m.joined_at)}</td>
                <td>{admin && <button className="link" onClick={() => act(api(`/members/${m.user_id}`, { method: "DELETE" }), "Member removed.")}>Remove</button>}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}</Load>
        {admin && (
          <div className="row" style={{ marginTop: 14 }}>
            <input placeholder="Full name" value={invite.full_name} onChange={(e) => setInvite({ ...invite, full_name: e.target.value })} />
            <input placeholder="Email" type="email" value={invite.email} onChange={(e) => setInvite({ ...invite, email: e.target.value })} />
            <select value={invite.role} onChange={(e) => setInvite({ ...invite, role: e.target.value as Role })}>{["VIEWER", "OPERATOR", "ADMIN"].map((r) => <option key={r} value={r}>{r.toLowerCase()}</option>)}</select>
            <button className="primary" onClick={() => act(api("/members", { method: "POST", body: invite }), "Invitation sent. The link to set a password is delivered by the server's mailer.")}>Invite</button>
          </div>
        )}
      </Card>
      <Card title="Clusters">
        {clusters.length === 0 ? <div className="empty">No clusters registered.</div> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Pooler</th><th>Database</th><th>Primary (observed by the collector)</th><th>Status</th></tr></thead>
            <tbody>{clusters.map((c) => (
              <tr key={c.id}><td><strong>{c.name}</strong></td><td className="mono">{c.pooler_host}:{c.pooler_port}</td><td className="mono">{c.database_name}</td>
                <td className="mono">{c.primary_host ?? <span className="muted">not set</span>}</td><td><Badge>{c.status}</Badge></td></tr>
            ))}</tbody>
          </table></div>
        )}
        {admin && (
          <div className="row" style={{ marginTop: 14 }}>
            <input placeholder="name" value={cluster.name} onChange={(e) => setCluster({ ...cluster, name: e.target.value })} style={{ width: 130 }} />
            <input placeholder="pooler host" value={cluster.pooler_host} onChange={(e) => setCluster({ ...cluster, pooler_host: e.target.value })} style={{ width: 150 }} />
            <input placeholder="primary host" value={cluster.primary_host} onChange={(e) => setCluster({ ...cluster, primary_host: e.target.value })} style={{ width: 150 }} />
            <input placeholder="database" value={cluster.database_name} onChange={(e) => setCluster({ ...cluster, database_name: e.target.value })} style={{ width: 110 }} />
            <button className="primary" onClick={() => act(api("/clusters", { method: "POST", body: { ...cluster, primary_host: cluster.primary_host || null } }).then(() => window.location.reload()), "Cluster registered.")}>Register cluster</button>
          </div>
        )}
      </Card>
    </div>
  );
}
