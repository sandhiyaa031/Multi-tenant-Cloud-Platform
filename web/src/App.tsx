import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { api, getToken, setToken, useApi, type Cluster, type Org, type Session, type Tenant } from "./api";
import { tenantColors } from "./ui";
import { Architecture, Forgot, Landing, Login, Product, Reset, Signup } from "./pages/Public";
import { Health, Overview, Queries, Tenants, Workloads } from "./pages/Observe";
import { AgentConsole, Canaries, Experiments, ProposalPage, Recommendations, TwinLab, Verification } from "./pages/Optimise";
import { Audit, Settings } from "./pages/Admin";

interface Me { user_id: string; email: string; full_name: string; org: Org }
interface AuthCtx { me: Me | null; ready: boolean; signIn: (s: Session) => void; signOut: () => void; refresh: () => void }
const Auth = createContext<AuthCtx>(null!);
export const useAuth = () => useContext(Auth);

interface ClusterCtx { clusters: Cluster[]; cluster: Cluster | null; select: (id: string) => void; tenants: Tenant[]; colors: Record<string, string>; reloadTenants: () => void }
const ClusterContext = createContext<ClusterCtx>(null!);
export const useCluster = () => useContext(ClusterContext);

function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const refresh = useCallback(() => {
    if (!getToken()) { setMe(null); setReady(true); return; }
    api<Me>("/auth/me").then(setMe).catch(() => setMe(null)).finally(() => setReady(true));
  }, []);
  useEffect(() => {
    refresh();
    const onLogout = () => setMe(null);
    window.addEventListener("dbpilot:logout", onLogout);
    return () => window.removeEventListener("dbpilot:logout", onLogout);
  }, [refresh]);
  const value = useMemo<AuthCtx>(() => ({
    me, ready, refresh,
    signIn: (s) => { setToken(s.access_token); refresh(); },
    signOut: () => { setToken(null); setMe(null); },
  }), [me, ready, refresh]);
  return <Auth.Provider value={value}>{children}</Auth.Provider>;
}

const NAV: [string, [string, string][]][] = [
  ["Observe", [["overview", "Overview"], ["tenants", "Tenants"], ["workloads", "Workloads"], ["queries", "Query Intelligence"], ["health", "Database Health"]]],
  ["Optimise", [["agent", "Agent Console"], ["recommendations", "Recommendations"], ["twin", "Digital Twin Lab"], ["verification", "Verification"], ["canary", "Canary Deployments"]]],
  ["Evidence", [["experiments", "Experiments"], ["audit", "Audit History"]]],
  ["Organization", [["settings", "Settings"]]],
];

function Shell() {
  const { me, ready, signOut } = useAuth();
  const navigate = useNavigate();
  const clusters = useApi<Cluster[]>(me ? "/clusters" : null);
  const [selected, setSelected] = useState<string | null>(localStorage.getItem("dbpilot.cluster"));
  const list = clusters.data ?? [];
  // Prefer a cluster the collector can reach; fall back to the first.
  const cluster = list.find((c) => c.id === selected) ?? list.find((c) => c.primary_host) ?? list[0] ?? null;
  const tenants = useApi<Tenant[]>(cluster ? `/tenants?cluster_id=${cluster.id}` : null);
  const ctx = useMemo<ClusterCtx>(() => ({
    clusters: list, cluster, tenants: tenants.data ?? [],
    colors: tenantColors((tenants.data ?? []).map((t) => t.db_role)),
    select: (id) => { localStorage.setItem("dbpilot.cluster", id); setSelected(id); },
    reloadTenants: tenants.reload,
  }), [list, cluster, tenants.data, tenants.reload]);

  if (!ready) return null;
  if (!me) return <Navigate to="/login" replace />;
  return (
    <ClusterContext.Provider value={ctx}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand"><span className="brand-mark">◆</span>DBPilot</div>
          <nav className="nav">
            {NAV.map(([group, items]) => (
              <div key={group}>
                <div className="nav-group">{group}</div>
                {items.map(([to, label]) => <NavLink key={to} to={`/app/${to}`}>{label}</NavLink>)}
              </div>
            ))}
          </nav>
        </aside>
        <div className="main">
          <header className="topbar">
            <div className="row">
              <span className="muted">Cluster</span>
              {list.length === 0 ? <span className="muted">none registered</span> : (
                <select value={cluster?.id ?? ""} onChange={(e) => ctx.select(e.target.value)}>
                  {list.map((c) => <option key={c.id} value={c.id}>{c.name}{c.primary_host ? "" : " (not observed)"}</option>)}
                </select>
              )}
            </div>
            <div className="who">
              <span>{me.org.name}</span><span className="badge info"><span className="dot" />{me.org.role.toLowerCase()}</span>
              <span className="muted">{me.email}</span>
              <button onClick={() => { signOut(); navigate("/"); }}>Sign out</button>
            </div>
          </header>
          <main className="page"><Outlet /></main>
        </div>
      </div>
    </ClusterContext.Provider>
  );
}

// Pages that only make sense for one cluster render this when there is none.
export function NeedCluster({ children }: { children: (cluster: Cluster) => ReactNode }) {
  const { cluster } = useCluster();
  if (!cluster) return <div className="empty">No cluster is registered for this organization yet. An admin can add one under Settings.</div>;
  return <>{children(cluster)}</>;
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/product" element={<Product />} />
        <Route path="/architecture" element={<Architecture />} />
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
        <Route path="/forgot-password" element={<Forgot />} />
        <Route path="/reset-password" element={<Reset />} />
        <Route path="/app" element={<Shell />}>
          <Route index element={<Navigate to="overview" replace />} />
          <Route path="overview" element={<Overview />} />
          <Route path="tenants" element={<Tenants />} />
          <Route path="workloads" element={<Workloads />} />
          <Route path="queries" element={<Queries />} />
          <Route path="health" element={<Health />} />
          <Route path="agent" element={<AgentConsole />} />
          <Route path="recommendations" element={<Recommendations />} />
          <Route path="recommendations/:id" element={<ProposalPage />} />
          <Route path="twin" element={<TwinLab />} />
          <Route path="verification" element={<Verification />} />
          <Route path="canary" element={<Canaries />} />
          <Route path="experiments" element={<Experiments />} />
          <Route path="audit" element={<Audit />} />
          <Route path="settings" element={<Settings />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthProvider>
  );
}
