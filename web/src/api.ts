// Thin client for the control-plane API. Every number the UI shows comes through here.
import { useCallback, useEffect, useRef, useState } from "react";

const BASE = "/api/v1";
const TOKEN_KEY = "dbpilot.token";
const NOTICE_KEY = "dbpilot.notice";
// Reads and clears the reason the last session ended, if it ended by itself.
export function takeSessionNotice(): string | null {
  const notice = sessionStorage.getItem(NOTICE_KEY);
  sessionStorage.removeItem(NOTICE_KEY);
  return notice;
}
export interface PublicConfig { signup_enabled: boolean; mail_delivery: boolean }

export type Role = "VIEWER" | "OPERATOR" | "ADMIN";
export interface Org { id: string; slug: string; name: string; role: Role }
export interface Session { access_token: string; org: Org; orgs: Org[] }
export interface Cluster { id: string; name: string; status: string; pooler_host: string; pooler_port: number; database_name: string; primary_host: string | null }
export interface Tenant { id: string; cluster_id: string; name: string; db_role: string; warehouse_lo: number; warehouse_hi: number; profile: string }
export interface Proposal {
  id: string; cluster_id: string; target_tenant_id: string | null; source: string; action: Record<string, any>;
  rationale: string; evidence: Record<string, any>; gate_mode: string; verification: string; auto_approve: boolean;
  state: string; state_reason: string; created_at: string; updated_at: string;
}
export interface Step { tier: string; decision: string; summary: string; detail: Record<string, any>; seconds: number; created_at: string }
export interface ProposalDetail extends Proposal { steps: Step[]; twin_runs: any[]; canary: any | null }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (t: string | null) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY));

export async function api<T = any>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(BASE + path, { method: init.method ?? "GET", headers, body: init.body === undefined ? undefined : JSON.stringify(init.body) });
  } catch {
    throw new ApiError(0, "Cannot reach the DBPilot API. Check that the stack is running.");
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON error body */ }
  if (!res.ok) {
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d: any) => d.msg).join("; ")
      : res.status >= 502 ? "The DBPilot API is not responding. Check that the stack is running." : res.statusText || `request failed (${res.status})`;
    // A token the server no longer accepts: the session has ended. Say so on the sign-in page.
    if (res.status === 401 && token) { setToken(null); sessionStorage.setItem(NOTICE_KEY, "expired"); window.dispatchEvent(new Event("dbpilot:logout")); }
    throw new ApiError(res.status, message);
  }
  return data as T;
}

export interface Loaded<T> { data: T | undefined; error: string | null; loading: boolean; reload: () => void }

// Fetches `path` and, if `everyMs` is set, keeps it fresh. A null path means "not ready yet".
export function useApi<T = any>(path: string | null, everyMs?: number): Loaded<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const current = useRef(path);
  current.current = path;

  const load = useCallback(() => {
    if (path === null) return;
    api<T>(path)
      .then((d) => { if (current.current === path) { setData(d); setError(null); } })
      .catch((e: Error) => { if (current.current === path) setError(e.message); })
      .finally(() => { if (current.current === path) setLoading(false); });
  }, [path]);

  useEffect(() => {
    setData(undefined); setError(null); setLoading(path !== null);
    load();
    if (!everyMs || path === null) return;
    const id = setInterval(load, everyMs);
    return () => clearInterval(id);
  }, [path, everyMs, load]);

  return { data, error, loading, reload: load };
}

export const rank: Record<Role, number> = { VIEWER: 0, OPERATOR: 1, ADMIN: 2 };
export const can = (role: Role | undefined, min: Role) => !!role && rank[role] >= rank[min];

export const fmt = {
  ms: (v: number | null | undefined) => (v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(v < 10 ? 2 : 1)} ms`),
  n: (v: number | null | undefined) => (v == null ? "–" : Intl.NumberFormat("en", { notation: v >= 100000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(v)),
  bytes: (v: number | null | undefined) => {
    if (v == null) return "–";
    const units = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let x = Math.abs(v);
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return `${v < 0 ? "-" : ""}${x.toFixed(x < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  },
  pct: (v: number | null | undefined, digits = 0) => (v == null ? "–" : `${(v * 100).toFixed(digits)}%`),
  ratio: (v: number | null | undefined) => (v == null ? "–" : `${v.toFixed(2)}×`),
  time: (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  datetime: (iso: string) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }),
};

// One sentence describing a typed action, for lists and headings.
export function describeAction(a: Record<string, any>): string {
  switch (a.type) {
    case "create_index": return `Create index on ${a.table} (${a.columns.join(", ")})${a.tenant_role ? ` for ${a.tenant_role}` : " for all tenants"}`;
    case "drop_index": return `Drop index ${a.index_name}`;
    case "role_setting": return `Set ${a.name} = ${a.value} for ${a.tenant_role}`;
    case "instance_setting": return `Set ${a.name} = ${a.value} for the whole instance`;
    case "analyze": return `Refresh statistics of ${a.table}${a.tenant_role ? ` (${a.tenant_role})` : ""}`;
    case "concurrency_cap": return `Cap ${a.tenant_role} at ${a.max_connections} connections`;
    case "replica_routing": return `${a.enabled ? "Route" : "Stop routing"} ${a.tenant_role} reads to the replica`;
    case "query_rewrite": return `Rewrite advice for query ${a.queryid}`;
    case "no_action": return "No action";
    default: return a.type;
  }
}
