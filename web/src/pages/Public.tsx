import { useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { api, takeSessionNotice, useApi, type PublicConfig, type Session } from "../api";
import { useAuth } from "../App";

// What this deployment offers before anyone is signed in. Until it has answered, nothing is hidden.
const usePublicConfig = () => useApi<PublicConfig>("/auth/config").data;

const LOOP = ["Observe", "Diagnose", "Plan", "Digital twin", "Verify", "Canary", "Monitor", "Rollback", "Learn"];

function PublicLayout({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const config = usePublicConfig();
  return (
    <div className="public">
      <nav className="public-nav">
        <Link to="/" className="brand" style={{ padding: 0 }}><span className="brand-mark">◆</span>DBPilot</Link>
        <div className="links">
          <Link to="/product">Product</Link>
          <Link to="/architecture">Architecture</Link>
          {me ? <Link to="/app" className="button primary">Open console</Link> : (
            <><Link to="/login">Sign in</Link>{config?.signup_enabled !== false && <Link to="/signup" className="button primary">Create account</Link>}</>
          )}
        </div>
      </nav>
      {children}
    </div>
  );
}

export function Landing() {
  const config = usePublicConfig();
  return (
    <PublicLayout>
      <header className="hero">
        <h1>Optimise a shared PostgreSQL without hurting a single tenant.</h1>
        <p>
          DBPilot lets an AI agent propose database optimisations, then proves each one on a real clone of your
          database, under your real workload, for every tenant, before production is touched.
        </p>
        <div className="loop">{LOOP.map((s) => <span key={s}>{s}</span>)}</div>
        <div className="row">
          {config?.signup_enabled === false ? <Link to="/login" className="button primary">Sign in</Link>
            : <Link to="/signup" className="button primary">Create an organization</Link>}
          <Link to="/product" className="button">How it works</Link>
        </div>
      </header>

      <section className="section">
        <h2>The problem with tuning a shared database</h2>
        <div className="grid cols-2">
          <div className="card"><h3>What helps one tenant</h3><p>An index that makes an analytics tenant's reports fast.</p><p>More sort memory for heavy queries.</p><p>More parallel workers per query.</p></div>
          <div className="card"><h3>Can hurt the others</h3><p>Every write to that table now maintains the index.</p><p>Less memory left when many small transactions run at once.</p><p>Fewer CPU cores for latency-sensitive transactions.</p></div>
        </div>
        <p className="secondary" style={{ marginTop: 16 }}>
          Judged on the whole workload, such a change can look like a win while one tenant quietly gets worse.
          DBPilot judges it per tenant.
        </p>
      </section>

      <section className="section">
        <h2>Four rules it never breaks</h2>
        <div className="grid cols-2">
          <div className="card"><h3>The agent never runs SQL</h3><p className="secondary">It picks from a closed set of typed actions. A deterministic executor turns an approved action into statements.</p></div>
          <div className="card"><h3>The agent cannot approve itself</h3><p className="secondary">Verification is separate code. A proposal is only data until it passes.</p></div>
          <div className="card"><h3>Every tenant is measured</h3><p className="secondary">Not just the one being helped. A change is rejected when tenant A improves and tenant B is harmed.</p></div>
          <div className="card"><h3>Uncertain is not safe</h3><p className="secondary">A result that cannot be told apart from harm is escalated to a person, never applied automatically.</p></div>
        </div>
      </section>
    </PublicLayout>
  );
}

export function Product() {
  const steps: [string, string][] = [
    ["Observe", "A collector reads PostgreSQL's own statistics every interval and stores what each tenant ran, how long it took and how its latency compares with its objective."],
    ["Diagnose and plan", "A rule-based proposer or an LLM agent inspects that telemetry through read-only tools and proposes one typed action: an index for one tenant, a setting for one role, a concurrency cap."],
    ["Digital twin", "A standby that deliberately trails production is cloned twice. The action is applied to one clone. The last minutes of captured production workload are replayed against both."],
    ["Verify", "The gate compares the two replays per tenant. The target must be shown to benefit; every other tenant must be shown not to be harmed. Storage and write-volume budgets apply."],
    ["Canary and monitor", "An approved change is applied to production under a contract derived from the twin's prediction. Each tenant's latency is watched against it."],
    ["Rollback", "If a tenant breaches the contract in two of three windows, or telemetry is lost, the stored inverse of the action runs automatically."],
    ["Learn", "Prediction and outcome are stored side by side for every proposal, so the twin's accuracy is measured rather than assumed."],
  ];
  return (
    <PublicLayout>
      <header className="hero" style={{ paddingBottom: 16 }}>
        <h1>How DBPilot works</h1>
        <p>Seven steps from a telemetry reading to a change you can trust, each one recorded.</p>
      </header>
      <section className="section">
        <div className="timeline">
          {steps.map(([t, d]) => <div className="item info" key={t}><strong>{t}</strong><div className="secondary">{d}</div></div>)}
        </div>
      </section>
      <section className="section">
        <h2>What an organization gets</h2>
        <div className="grid cols-3">
          <div className="card"><h3>Visibility</h3><p className="secondary">Per-tenant latency, load, the most expensive queries with their plans, and database health.</p></div>
          <div className="card"><h3>Control</h3><p className="secondary">Admin, operator and viewer roles. Operators approve or reject; nothing verified as uncertain is applied without an admin.</p></div>
          <div className="card"><h3>Accountability</h3><p className="secondary">An append-only audit log, and a ledger of every proposal with what was predicted and what happened.</p></div>
        </div>
      </section>
    </PublicLayout>
  );
}

export function Architecture() {
  return (
    <PublicLayout>
      <header className="hero" style={{ paddingBottom: 16 }}>
        <h1>Three planes</h1>
        <p>Experimenting on a database is real load. DBPilot keeps it away from the thing it is measuring.</p>
      </header>
      <section className="section">
        <div className="planes">
          <div className="card"><h3>Control plane — decides</h3><p className="secondary">API with authentication and role checks, the control database, the telemetry collector, the agent, the verification engine and the canary controller.</p></div>
          <div className="card"><h3>Data plane — serves tenants</h3><p className="secondary">PgBouncer in front of a PostgreSQL primary and replica. Tenants share tables; each has its own role, its own partitions and row-level security.</p></div>
          <div className="card"><h3>Experimentation plane — tests</h3><p className="secondary">A delayed standby as twin source, and ephemeral control and treatment clones on their own cores, driven by a node agent.</p></div>
        </div>
      </section>
      <section className="section">
        <h2>How tenants are kept apart and observable</h2>
        <div className="grid cols-3">
          <div className="card"><h3>A role per tenant</h3><p className="secondary">PostgreSQL attributes statistics to roles, so every query is attributed to a tenant with no proxy.</p></div>
          <div className="card"><h3>Row-level security</h3><p className="secondary">A tenant's role can only see and write its own warehouse range, enforced by the database.</p></div>
          <div className="card"><h3>A partition per tenant</h3><p className="secondary">An index can be built for one tenant alone, so its write cost falls on that tenant only.</p></div>
        </div>
      </section>
      <section className="section">
        <h2>Verification tiers</h2>
        <table>
          <thead><tr><th>Tier</th><th>Check</th><th>Cost</th></tr></thead>
          <tbody>
            <tr><td>T0</td><td>Static rules: valid action, within bounds, memory arithmetic, no duplicate</td><td>milliseconds</td></tr>
            <tr><td>T1</td><td>Planner what-if with a hypothetical index</td><td>seconds</td></tr>
            <tr><td>T2</td><td>Digital twin replay and the per-tenant gate</td><td>minutes</td></tr>
            <tr><td>T3</td><td>Canary in production under a rollback contract</td><td>minutes</td></tr>
          </tbody>
        </table>
      </section>
    </PublicLayout>
  );
}

function AuthCard({ title, children, footer }: { title: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <PublicLayout>
      <div className="auth card">
        <h1 style={{ marginBottom: 18 }}>{title}</h1>
        {children}
        {footer && <div className="secondary" style={{ marginTop: 16 }}>{footer}</div>}
      </div>
    </PublicLayout>
  );
}

function useSubmit<T>(fn: () => Promise<T>, done: (r: T) => void) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(null);
    fn().then(done).catch((err: Error) => setError(err.message)).finally(() => setBusy(false));
  };
  return { submit, error, busy };
}

export function Login() {
  const { me, signIn } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const config = usePublicConfig();
  // Only ever return to a page of this console, never to an address someone put in a link.
  const next = params.get("next")?.startsWith("/app") ? params.get("next")! : "/app";
  const [notice] = useState(takeSessionNotice);
  const [email, setEmail] = useState(""); const [password, setPassword] = useState("");
  const f = useSubmit(() => api<Session>("/auth/login", { method: "POST", body: { email, password } }), (s) => { signIn(s); navigate(next); });
  if (me) return <Navigate to={next} replace />;
  return (
    <AuthCard title="Sign in" footer={config?.signup_enabled === false ? null : <>New here? <Link to="/signup">Create an organization</Link></>}>
      {notice === "expired" && <p className="notice" role="status">Your session has ended. Sign in again to continue where you were.</p>}
      <form onSubmit={f.submit}>
        <label className="field"><span>Email</span><input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
        <label className="field"><span>Password</span><input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {f.error && <p className="error" role="alert">{f.error}</p>}
        <div className="spread"><button className="primary" disabled={f.busy}>{f.busy ? "Signing in…" : "Sign in"}</button><Link to="/forgot-password">Forgot password?</Link></div>
      </form>
    </AuthCard>
  );
}

export function Signup() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ org_name: "", full_name: "", email: "", password: "" });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  const f = useSubmit(() => api<Session>("/auth/signup", { method: "POST", body: form }), (s) => { signIn(s); navigate("/app/settings"); });
  const config = usePublicConfig();
  if (config?.signup_enabled === false) return (
    <AuthCard title="Create an organization" footer={<Link to="/login">Back to sign in</Link>}>
      <p className="notice">Creating organizations is switched off on this deployment. Ask an admin of an existing organization to invite you.</p>
    </AuthCard>
  );
  return (
    <AuthCard title="Create an organization" footer={<>Already have an account? <Link to="/login">Sign in</Link></>}>
      <p className="notice">
        A new organization starts empty. DBPilot observes databases it has been given access to; on this deployment that is
        the demo data plane, which you register under Settings after signing up.
      </p>
      <form onSubmit={f.submit}>
        <label className="field"><span>Organization name</span><input required minLength={2} value={form.org_name} onChange={set("org_name")} /></label>
        <label className="field"><span>Your name</span><input required value={form.full_name} onChange={set("full_name")} /></label>
        <label className="field"><span>Email</span><input type="email" autoComplete="username" required value={form.email} onChange={set("email")} /></label>
        <label className="field"><span>Password (at least 10 characters)</span><input type="password" autoComplete="new-password" required minLength={10} value={form.password} onChange={set("password")} /></label>
        {f.error && <p className="error">{f.error}</p>}
        <button className="primary" disabled={f.busy}>Create organization</button>
        <p className="muted" style={{ marginTop: 12 }}>You become its first admin. No confirmation email is sent.</p>
      </form>
    </AuthCard>
  );
}

export function Forgot() {
  const [email, setEmail] = useState(""); const [sent, setSent] = useState(false);
  const f = useSubmit(() => api("/auth/forgot-password", { method: "POST", body: { email } }), () => setSent(true));
  const config = usePublicConfig();
  // Without a mail server a link cannot reach anyone; say who can help instead of pretending.
  if (config && !config.mail_delivery) return (
    <AuthCard title="Reset your password" footer={<Link to="/login">Back to sign in</Link>}>
      <p className="notice">
        This deployment has no mail server, so a reset link cannot be emailed. Ask an admin of your organization to
        create a password link for you under Settings → Members.
      </p>
    </AuthCard>
  );
  return (
    <AuthCard title="Reset your password" footer={<Link to="/login">Back to sign in</Link>}>
      {sent ? <p className="notice">If that address has an account, a reset link has been sent. It expires in 30 minutes.</p> : (
        <form onSubmit={f.submit}>
          <label className="field"><span>Email</span><input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
          {f.error && <p className="error">{f.error}</p>}
          <button className="primary" disabled={f.busy}>Send reset link</button>
        </form>
      )}
    </AuthCard>
  );
}

export function Reset() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(false);
  const f = useSubmit(() => api("/auth/reset-password", { method: "POST", body: { token, new_password: password } }), () => setDone(true));
  if (done) return (
    <AuthCard title="Password set">
      <p className="notice" role="status">Your password has been set.</p>
      <button className="primary" onClick={() => navigate("/login")}>Go to sign in</button>
    </AuthCard>
  );
  return (
    <AuthCard title="Choose a new password">
      {!token ? <p className="error">This link is missing its token. Request a new one.</p> : (
        <form onSubmit={f.submit}>
          <label className="field"><span>New password (at least 10 characters)</span><input type="password" autoComplete="new-password" required minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          {f.error && <p className="error">{f.error}</p>}
          <button className="primary" disabled={f.busy}>Set password</button>
        </form>
      )}
    </AuthCard>
  );
}
