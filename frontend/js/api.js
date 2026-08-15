/**
 * api.js — every HTTP call to the Flask backend goes through here.
 * Nothing else in the app calls fetch() directly. This is what makes
 * the data flow explainable in one place:
 *
 *   app.js calls e.g. API.listCompanies()
 *     -> fetch("http://.../companies")
 *     -> Flask route (platform_api/companies.py)
 *     -> psycopg -> PostgreSQL
 *     -> JSON response
 *     -> parsed here and returned to app.js
 */

const API_BASE = "http://127.0.0.1:5000";

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (networkErr) {
    throw new ApiError("Cannot reach backend at " + API_BASE, 0, null);
  }

  let body = null;
  try { body = await res.json(); } catch (_) { /* empty body */ }

  if (!res.ok) {
    const message = (body && body.message) || `Request failed (${res.status})`;
    throw new ApiError(message, res.status, body);
  }
  return body;
}

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

const API = {
  health: () => request("/health"),

  // Companies (full CRUD)
  listCompanies: (includeInactive = false) =>
    request(`/companies${includeInactive ? "?include_inactive=true" : ""}`),
  getCompany: (id) => request(`/companies/${id}`),
  createCompany: (data) =>
    request("/companies", { method: "POST", body: JSON.stringify(data) }),
  updateCompany: (id, data) =>
    request(`/companies/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deactivateCompany: (id) =>
    request(`/companies/${id}`, { method: "DELETE" }),

  // Reads
  listTeams: (companyId) =>
    request(`/teams${companyId ? "?company_id=" + companyId : ""}`),
  listUsers: (companyId) =>
    request(`/users${companyId ? "?company_id=" + companyId : ""}`),
  listSubscriptions: (companyId, includeHistory = false) => {
    const params = new URLSearchParams();
    if (companyId) params.set("company_id", companyId);
    if (includeHistory) params.set("include_history", "true");
    const qs = params.toString();
    return request(`/subscriptions${qs ? "?" + qs : ""}`);
  },
  listDatabaseInstances: (companyId) =>
    request(`/database-instances${companyId ? "?company_id=" + companyId : ""}`),
  listAlerts: (companyId, unacknowledgedOnly = false) => {
    const params = new URLSearchParams();
    if (companyId) params.set("company_id", companyId);
    if (unacknowledgedOnly) params.set("unacknowledged", "true");
    const qs = params.toString();
    return request(`/alerts${qs ? "?" + qs : ""}`);
  },
  listQueryPerformance: (companyId, limit = 100) => {
    const params = new URLSearchParams();
    if (companyId) params.set("company_id", companyId);
    params.set("limit", limit);
    return request(`/query-performance?${params.toString()}`);
  },
  listCompanyHealth: () => request("/company-health"),
  getCompanyHealth: (id) => request(`/company-health/${id}`),
  listPolicies: (companyId, currentOnly = true) => {
    const params = new URLSearchParams();
    if (companyId) params.set("company_id", companyId);
    if (!currentOnly) params.set("current_only", "false");
    const qs = params.toString();
    return request(`/policies${qs ? "?" + qs : ""}`);
  },
};
