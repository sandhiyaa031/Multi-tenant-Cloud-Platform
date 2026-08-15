/**
 * app.js — routing + page rendering + interactions.
 * Flow for every page: hash changes -> render<Page>() -> API.xxx()
 * -> build HTML string from the JSON -> content.innerHTML = html.
 */

const content = document.getElementById("content");
const pageTitle = document.getElementById("page-title");
const navLinks = [...document.querySelectorAll(".nav-item")];

let companiesCache = [];          // [{company_id, name, ...}]
let companiesMap = new Map();     // id -> name

// ───────────────────────── helpers ─────────────────────────

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function fmtNum(n, decimals = 1) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: decimals });
}

function companyName(id) {
  return companiesMap.get(id) || (id ? `#${id}` : "—");
}

function severityBadge(sev) {
  const cls = { critical: "badge-crit", warning: "badge-warn", info: "badge-info" }[sev] || "badge-neutral";
  return `<span class="badge ${cls}">${esc(sev || "—")}</span>`;
}

function statusBadge(status) {
  const cls = { active: "badge-ok", paused: "badge-warn", decommissioned: "badge-crit" }[status] || "badge-neutral";
  return `<span class="badge ${cls}">${esc(status || "—")}</span>`;
}

function ackBadge(row) {
  return row.acknowledged_at
    ? `<span class="badge badge-ok">acknowledged</span>`
    : `<span class="badge badge-warn">open</span>`;
}

function loadingHtml(label = "Loading…") {
  return `<div class="state-msg"><span class="spinner"></span>${esc(label)}</div>`;
}
function emptyHtml(label = "No data yet.") {
  return `<div class="state-msg">${esc(label)}</div>`;
}
function errorHtml(err) {
  const msg = err instanceof ApiError ? err.message : String(err);
  return `<div class="state-msg error">Failed to load: ${esc(msg)}</div>`;
}

function showToast(message, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = message;
  t.className = "toast show" + (isError ? " error" : "");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { t.className = "toast"; }, 3200);
}

// select options builder for company filters
function companyOptions(selectedId) {
  let opts = `<option value="">All companies</option>`;
  for (const c of companiesCache) {
    opts += `<option value="${c.company_id}" ${String(c.company_id) === String(selectedId) ? "selected" : ""}>${esc(c.name)}</option>`;
  }
  return opts;
}

// ───────────────────────── connection status ─────────────────────────

async function checkBackend() {
  const dot = document.getElementById("conn-status");
  const label = document.getElementById("conn-status-label");
  try {
    await API.health();
    dot.className = "status-dot ok";
    label.textContent = "connected";
  } catch (e) {
    dot.className = "status-dot err";
    label.textContent = "backend unreachable";
  }
}

async function loadCompaniesCache() {
  try {
    companiesCache = (await API.listCompanies(true)).companies || [];
    companiesMap = new Map(companiesCache.map((c) => [c.company_id, c.name]));
  } catch (e) {
    companiesCache = [];
    companiesMap = new Map();
  }
}

// ───────────────────────── simple SVG line chart ─────────────────────────

function renderLineChart(points, { valueKey, labelKey, height = 180 }) {
  if (!points.length) return emptyHtml("No query activity to chart yet.");
  const w = 900, h = height, padL = 30, padB = 20, padT = 10, padR = 10;
  const values = points.map((p) => Number(p[valueKey]) || 0);
  const maxV = Math.max(...values, 1);
  const stepX = (w - padL - padR) / Math.max(points.length - 1, 1);

  const coord = (i, v) => {
    const x = padL + i * stepX;
    const y = padT + (h - padT - padB) * (1 - v / maxV);
    return [x, y];
  };

  const linePts = values.map((v, i) => coord(i, v).join(","));
  const areaPts = [`${padL},${h - padB}`, ...linePts, `${padL + (points.length - 1) * stepX},${h - padB}`];

  const yTicks = [0, 0.5, 1].map((f) => {
    const y = padT + (h - padT - padB) * (1 - f);
    return `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" class="chart-axis" stroke-dasharray="2,3"/>
             <text x="4" y="${y + 3}" class="chart-label">${fmtNum(maxV * f, 0)}</text>`;
  }).join("");

  const xLabels = points.length > 1
    ? [0, Math.floor(points.length / 2), points.length - 1].map((i) => {
        const [x] = coord(i, values[i]);
        return `<text x="${x}" y="${h - 4}" class="chart-label" text-anchor="middle">${esc(points[i][labelKey])}</text>`;
      }).join("")
    : "";

  return `
    <svg class="chart-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.5"/>
          <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
        </linearGradient>
      </defs>
      ${yTicks}
      <polygon class="chart-area" points="${areaPts.join(" ")}"/>
      <polyline class="chart-line" points="${linePts.join(" ")}"/>
      ${xLabels}
    </svg>`;
}

// ───────────────────────── PAGE: Overview ─────────────────────────

async function renderOverview() {
  content.innerHTML = loadingHtml("Loading overview…");
  try {
    const [health, alerts, instances, perf] = await Promise.all([
      API.listCompanyHealth(),
      API.listAlerts(null, true),
      API.listDatabaseInstances(),
      API.listQueryPerformance(null, 200),
    ]);

    const healthRows = health.company_health || [];
    const alertRows = alerts.alerts || [];
    const instanceRows = instances.database_instances || [];
    const perfRows = (perf.query_performance || []).slice().reverse(); // chronological

    const totalQueries = healthRows.reduce((s, r) => s + (r.queries_last_24h || 0), 0);
    const avgLatency = perfRows.length
      ? perfRows.reduce((s, r) => s + (Number(r.execution_time_ms) || 0), 0) / perfRows.length
      : null;
    const criticalAlerts = alertRows.filter((a) => a.severity === "critical").length;

    const chartPoints = perfRows.map((r) => ({
      execution_time_ms: r.execution_time_ms,
      t: fmtDate(r.executed_at).split(",")[1]?.trim() || fmtDate(r.executed_at),
    }));

    content.innerHTML = `
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Total Queries (24h)</div>
          <div class="kpi-value">${fmtNum(totalQueries, 0)}</div>
          <div class="kpi-sub">across ${healthRows.length} companies</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Avg Query Latency</div>
          <div class="kpi-value">${avgLatency !== null ? fmtNum(avgLatency, 1) + " ms" : "—"}</div>
          <div class="kpi-sub">recent ${perfRows.length}-query sample</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Active Alerts</div>
          <div class="kpi-value">${alertRows.length}</div>
          <div class="kpi-sub">${criticalAlerts} critical</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Database Instances</div>
          <div class="kpi-value">${instanceRows.length}</div>
          <div class="kpi-sub">${instanceRows.filter((i) => i.status === "active").length} active</div>
        </div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-header">
            <span class="card-title">Query Execution Latency</span>
            <span class="card-meta">most recent ${perfRows.length} queries</span>
          </div>
          ${renderLineChart(chartPoints, { valueKey: "execution_time_ms", labelKey: "t" })}
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Company Health</span></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Company</th><th class="num">Queries/24h</th><th class="num">Alerts</th><th class="num">Unhealthy Replicas</th></tr></thead>
              <tbody>
                ${healthRows.length ? healthRows.map((r) => `
                  <tr>
                    <td>${esc(r.company_name)}</td>
                    <td class="num">${fmtNum(r.queries_last_24h, 0)}</td>
                    <td class="num">${r.unacknowledged_alerts_total > 0 ? `<span class="badge badge-warn">${r.unacknowledged_alerts_total}</span>` : "0"}</td>
                    <td class="num">${r.unhealthy_replicas > 0 ? `<span class="badge badge-crit">${r.unhealthy_replicas}</span>` : "0"}</td>
                  </tr>`).join("") : `<tr><td colspan="4">${emptyHtml("No companies yet.")}</td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-header"><span class="card-title">Recent / Critical Alerts</span></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Severity</th><th>Message</th><th>Company</th><th>When</th></tr></thead>
              <tbody>
                ${alertRows.slice(0, 8).length ? alertRows.slice(0, 8).map((a) => `
                  <tr class="${a.severity === "critical" ? "row-highlight" : ""}">
                    <td>${severityBadge(a.severity)}</td>
                    <td>${esc(a.message)}</td>
                    <td>${esc(companyName(a.company_id))}</td>
                    <td class="text-dim">${fmtDate(a.acknowledged_at || null) === "—" ? "open" : "acked"}</td>
                  </tr>`).join("") : `<tr><td colspan="4">${emptyHtml("No unacknowledged alerts.")}</td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Database Instances</span></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Instance</th><th>Company</th><th>Status</th></tr></thead>
              <tbody>
                ${instanceRows.length ? instanceRows.map((i) => `
                  <tr>
                    <td>${esc(i.instance_name)}</td>
                    <td>${esc(companyName(i.company_id))}</td>
                    <td>${statusBadge(i.status)}</td>
                  </tr>`).join("") : `<tr><td colspan="3">${emptyHtml("No instances registered.")}</td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

// ───────────────────────── PAGE: Databases ─────────────────────────

async function renderDatabases() {
  content.innerHTML = loadingHtml("Loading database instances…");
  try {
    const { database_instances } = await API.listDatabaseInstances();
    const rows = database_instances || [];

    content.innerHTML = `
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Instance</th><th>Company</th><th>Host</th><th>Port</th><th>Database</th><th>Status</th></tr></thead>
            <tbody id="db-instance-rows">
              ${rows.length ? rows.map((r) => `
                <tr class="clickable" data-id="${r.database_instance_id}">
                  <td>${esc(r.instance_name)}</td>
                  <td>${esc(companyName(r.company_id))}</td>
                  <td class="mono">${esc(r.host)}</td>
                  <td class="mono">${esc(r.port)}</td>
                  <td class="mono">${esc(r.db_name)}</td>
                  <td>${statusBadge(r.status)}</td>
                </tr>`).join("") : `<tr><td colspan="6">${emptyHtml("No database instances registered yet.")}</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
      <div class="text-faint" style="font-size:11.5px;">
        Replica health (primary/replica linkage, replication lag) isn't shown here — no backend
        endpoint exposes ReplicaDatabase yet. Flagged as a follow-up API, not fabricated.
      </div>
    `;

    document.querySelectorAll("#db-instance-rows tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const row = rows.find((r) => String(r.database_instance_id) === tr.dataset.id);
        openDetailPanel("Instance: " + row.instance_name, `
          <div class="detail-row"><span class="k">Company</span><span class="v">${esc(companyName(row.company_id))}</span></div>
          <div class="detail-row"><span class="k">Host</span><span class="v">${esc(row.host)}</span></div>
          <div class="detail-row"><span class="k">Port</span><span class="v">${esc(row.port)}</span></div>
          <div class="detail-row"><span class="k">Database</span><span class="v">${esc(row.db_name)}</span></div>
          <div class="detail-row"><span class="k">Status</span><span class="v">${statusBadge(row.status)}</span></div>
          <div class="detail-section-title">Replica</div>
          <div class="state-msg" style="padding:16px 0;">No replica API yet — see note on the Databases page.</div>
        `);
      });
    });
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

// ───────────────────────── PAGE: Query Performance ─────────────────────────

let qpRowsCache = [];

function qpFilterRows(rows, filters) {
  return rows.filter((r) => {
    if (filters.company && String(r.company_id) !== String(filters.company)) return false;
    if (filters.action && r.policy_action !== filters.action) return false;
    if (filters.highCost && !(Number(r.corrected_cost) > Number(filters.highCost))) return false;
    return true;
  });
}

async function renderQueryPerformance() {
  content.innerHTML = loadingHtml("Loading query performance…");
  try {
    const { query_performance } = await API.listQueryPerformance(null, 300);
    qpRowsCache = query_performance || [];
    drawQueryPerformance({});
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

function drawQueryPerformance(filters) {
  const rows = qpFilterRows(qpRowsCache, filters);
  const actions = [...new Set(qpRowsCache.map((r) => r.policy_action).filter(Boolean))];

  content.innerHTML = `
    <div class="filters">
      <select id="qp-company">${companyOptions(filters.company)}</select>
      <select id="qp-action">
        <option value="">All policy actions</option>
        ${actions.map((a) => `<option value="${esc(a)}" ${a === filters.action ? "selected" : ""}>${esc(a)}</option>`).join("")}
      </select>
      <input type="number" id="qp-cost" placeholder="Min corrected cost" value="${filters.highCost || ""}" style="width:150px;">
      <span class="card-meta">${rows.length} of ${qpRowsCache.length} queries</span>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Template</th><th>Company</th><th class="num">Exec (ms)</th><th class="num">Plan (ms)</th>
              <th class="num">Planner Est.</th><th class="num">Corrected</th><th class="num">Conf.</th>
              <th>Action</th><th>When</th>
            </tr>
          </thead>
          <tbody id="qp-rows">
            ${rows.length ? rows.map((r) => {
              const overEst = r.corrected_cost > r.planner_estimated_cost * 1.5;
              return `
              <tr class="clickable ${overEst ? "row-highlight" : ""}" data-id="${r.query_history_id}">
                <td class="mono">${esc(r.query_template_hash)}</td>
                <td>${esc(r.company_name)}</td>
                <td class="num">${fmtNum(r.execution_time_ms)}</td>
                <td class="num">${fmtNum(r.planning_time_ms)}</td>
                <td class="num text-dim">${fmtNum(r.planner_estimated_cost, 0)}</td>
                <td class="num">${fmtNum(r.corrected_cost, 0)}</td>
                <td class="num">${r.confidence_score !== null ? fmtNum(r.confidence_score * 100, 0) + "%" : "—"}</td>
                <td>${r.policy_action ? `<span class="badge badge-info">${esc(r.policy_action)}</span>` : "—"}</td>
                <td class="text-dim">${fmtDate(r.executed_at)}</td>
              </tr>`;
            }).join("") : `<tr><td colspan="9">${emptyHtml("No queries match these filters.")}</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
  `;

  const collect = () => ({
    company: document.getElementById("qp-company").value,
    action: document.getElementById("qp-action").value,
    highCost: document.getElementById("qp-cost").value,
  });
  ["qp-company", "qp-action"].forEach((id) =>
    document.getElementById(id).addEventListener("change", () => drawQueryPerformance(collect()))
  );
  document.getElementById("qp-cost").addEventListener("input", () => drawQueryPerformance(collect()));

  document.querySelectorAll("#qp-rows tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const row = rows.find((r) => String(r.query_history_id) === tr.dataset.id);
      const maxCost = Math.max(row.planner_estimated_cost || 0, row.corrected_cost || 0, 1);
      openDetailPanel("Query " + row.query_history_id, `
        <div class="detail-row"><span class="k">Template</span><span class="v">${esc(row.query_template_hash)}</span></div>
        <div class="detail-row"><span class="k">Company</span><span class="v">${esc(row.company_name)}</span></div>
        <div class="detail-row"><span class="k">Instance</span><span class="v">${esc(row.instance_name)}</span></div>
        <div class="detail-row"><span class="k">Executed</span><span class="v">${fmtDate(row.executed_at)}</span></div>

        <div class="detail-section-title">Execution Metrics</div>
        <div class="detail-row"><span class="k">Execution time</span><span class="v">${fmtNum(row.execution_time_ms)} ms</span></div>
        <div class="detail-row"><span class="k">Planning time</span><span class="v">${fmtNum(row.planning_time_ms)} ms</span></div>

        <div class="detail-section-title">Planner vs Corrected Cost</div>
        <div class="cost-compare">
          <div class="cost-row"><span class="cost-label">Planner est.</span>
            <div class="cost-bar-track"><div class="cost-bar-fill planner" style="width:${(row.planner_estimated_cost / maxCost) * 100}%"></div></div>
            <span class="cost-num">${fmtNum(row.planner_estimated_cost, 0)}</span></div>
          <div class="cost-row"><span class="cost-label">Corrected</span>
            <div class="cost-bar-track"><div class="cost-bar-fill corrected" style="width:${(row.corrected_cost / maxCost) * 100}%"></div></div>
            <span class="cost-num">${fmtNum(row.corrected_cost, 0)}</span></div>
        </div>
        <div class="detail-row"><span class="k">Confidence</span><span class="v">${row.confidence_score !== null ? fmtNum(row.confidence_score * 100, 0) + "%" : "—"}</span></div>
        <div class="detail-row"><span class="k">Cold start</span><span class="v">${row.is_cold_start ? "yes" : "no"}</span></div>

        <div class="detail-section-title">Policy Decision</div>
        <div class="detail-row"><span class="k">Action</span><span class="v">${esc(row.policy_action || "—")}</span></div>
        <div class="detail-row"><span class="k">Delay</span><span class="v">${fmtNum(row.policy_delay_ms, 0)} ms</span></div>
        <div class="detail-row"><span class="k">Replica</span><span class="v">${esc(row.routed_replica_host || "—")}</span></div>
      `);
    });
  });
}

// ───────────────────────── PAGE: Alerts ─────────────────────────

let alertRowsCache = [];

async function renderAlerts() {
  content.innerHTML = loadingHtml("Loading alerts…");
  try {
    const { alerts } = await API.listAlerts();
    alertRowsCache = alerts || [];
    drawAlerts({});
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

function drawAlerts(filters) {
  const rows = alertRowsCache.filter((a) => {
    if (filters.company && String(a.company_id) !== String(filters.company)) return false;
    if (filters.severity && a.severity !== filters.severity) return false;
    if (filters.ack === "open" && a.acknowledged_at) return false;
    if (filters.ack === "acked" && !a.acknowledged_at) return false;
    return true;
  });

  content.innerHTML = `
    <div class="filters">
      <select id="al-company">${companyOptions(filters.company)}</select>
      <select id="al-severity">
        <option value="">All severities</option>
        <option value="critical" ${filters.severity === "critical" ? "selected" : ""}>Critical</option>
        <option value="warning" ${filters.severity === "warning" ? "selected" : ""}>Warning</option>
        <option value="info" ${filters.severity === "info" ? "selected" : ""}>Info</option>
      </select>
      <select id="al-ack">
        <option value="">Open + Acknowledged</option>
        <option value="open" ${filters.ack === "open" ? "selected" : ""}>Open only</option>
        <option value="acked" ${filters.ack === "acked" ? "selected" : ""}>Acknowledged only</option>
      </select>
      <span class="card-meta">${rows.length} of ${alertRowsCache.length} alerts</span>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Severity</th><th>Message</th><th>Company</th><th>Source Query</th><th>State</th><th>Acknowledged</th></tr></thead>
          <tbody>
            ${rows.length ? rows.map((a) => `
              <tr class="${a.severity === "critical" && !a.acknowledged_at ? "row-highlight" : ""}">
                <td>${severityBadge(a.severity)}</td>
                <td>${esc(a.message)}</td>
                <td>${esc(companyName(a.company_id))}</td>
                <td class="mono text-dim">${a.source_query_history_id ? "#" + a.source_query_history_id : "—"}</td>
                <td>${ackBadge(a)}</td>
                <td class="text-dim">${a.acknowledged_at ? fmtDate(a.acknowledged_at) : "—"}</td>
              </tr>`).join("") : `<tr><td colspan="6">${emptyHtml("No alerts match these filters.")}</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
  `;

  const collect = () => ({
    company: document.getElementById("al-company").value,
    severity: document.getElementById("al-severity").value,
    ack: document.getElementById("al-ack").value,
  });
  ["al-company", "al-severity", "al-ack"].forEach((id) =>
    document.getElementById(id).addEventListener("change", () => drawAlerts(collect()))
  );
}

// ───────────────────────── PAGE: Policies ─────────────────────────

async function renderPolicies() {
  content.innerHTML = loadingHtml("Loading policies…");
  try {
    const { policies } = await API.listPolicies(null, true);
    const rows = policies || [];

    content.innerHTML = `
      <div class="text-faint" style="font-size:12px; margin-bottom:14px;">
        A QuotaPolicy sets, per company, the query-cost threshold above which Prediction triggers
        a governance Alert, and the delay threshold used for throttling decisions.
        Only the current policy (effective_to is open-ended) is shown per company.
      </div>
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Company</th><th class="num">Cost Threshold</th><th class="num">Delay Threshold (ms)</th><th>Effective From</th></tr></thead>
            <tbody>
              ${rows.length ? rows.map((p) => `
                <tr>
                  <td>${esc(p.company_name)}</td>
                  <td class="num mono">${fmtNum(p.cost_threshold, 0)}</td>
                  <td class="num mono">${fmtNum(p.delay_threshold_ms, 0)}</td>
                  <td class="text-dim">${fmtDate(p.effective_from)}</td>
                </tr>`).join("") : `<tr><td colspan="4">${emptyHtml("No governance policies configured yet.")}</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    `;
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

// ───────────────────────── PAGE: Teams ─────────────────────────

async function renderTeams() {
  content.innerHTML = loadingHtml("Loading teams…");
  try {
    const { teams } = await API.listTeams();
    const rows = teams || [];
    content.innerHTML = `
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Team</th><th>Company</th></tr></thead>
            <tbody>
              ${rows.length ? rows.map((t) => `
                <tr><td>${esc(t.team_name)}</td><td>${esc(companyName(t.company_id))}</td></tr>
              `).join("") : `<tr><td colspan="2">${emptyHtml("No teams yet.")}</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    `;
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

// ───────────────────────── PAGE: Subscriptions ─────────────────────────

async function renderSubscriptions() {
  content.innerHTML = loadingHtml("Loading subscriptions…");
  try {
    const { subscriptions } = await API.listSubscriptions();
    const rows = subscriptions || [];
    content.innerHTML = `
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Company</th><th>Plan</th><th class="num">Price/mo</th><th class="num">Max DBs</th><th class="num">Max Queries/Day</th><th>Start Date</th></tr></thead>
            <tbody>
              ${rows.length ? rows.map((s) => `
                <tr>
                  <td>${esc(s.company_name)}</td>
                  <td><span class="badge badge-info">${esc(s.plan_name)}</span></td>
                  <td class="num mono">$${fmtNum(s.price_monthly, 2)}</td>
                  <td class="num">${fmtNum(s.max_databases, 0)}</td>
                  <td class="num">${fmtNum(s.max_queries_per_day, 0)}</td>
                  <td class="text-dim">${fmtDate(s.start_date)}</td>
                </tr>`).join("") : `<tr><td colspan="6">${emptyHtml("No active subscriptions.")}</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    `;
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

// ───────────────────────── PAGE: Company Management (CRUD) ─────────────────────────

async function renderCompanies() {
  content.innerHTML = loadingHtml("Loading companies…");
  try {
    await drawCompanies();
  } catch (e) {
    content.innerHTML = errorHtml(e);
  }
}

async function drawCompanies() {
  const { companies } = await API.listCompanies(true);
  const rows = companies || [];

  content.innerHTML = `
    <div class="filters" style="justify-content:space-between;">
      <span class="card-meta">${rows.length} companies</span>
      <button class="btn btn-primary" id="add-company-btn">+ Add Company</button>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Industry</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
          <tbody id="company-rows">
            ${rows.length ? rows.map((c) => `
              <tr>
                <td>${esc(c.name)}</td>
                <td class="text-dim">${esc(c.industry || "—")}</td>
                <td>${c.is_active ? `<span class="badge badge-ok">active</span>` : `<span class="badge badge-neutral">inactive</span>`}</td>
                <td class="text-dim">${fmtDate(c.created_at)}</td>
                <td class="actions-cell">
                  <button class="btn btn-sm edit-btn" data-id="${c.company_id}">Edit</button>
                  ${c.is_active ? `<button class="btn btn-sm btn-danger deactivate-btn" data-id="${c.company_id}">Deactivate</button>` : ""}
                </td>
              </tr>`).join("") : `<tr><td colspan="5">${emptyHtml("No companies yet — add the first one.")}</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
  `;

  document.getElementById("add-company-btn").addEventListener("click", () => openCompanyModal(null));
  document.querySelectorAll(".edit-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      const c = rows.find((r) => String(r.company_id) === btn.dataset.id);
      openCompanyModal(c);
    })
  );
  document.querySelectorAll(".deactivate-btn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Deactivate this company? (soft delete — is_active becomes false)")) return;
      try {
        await API.deactivateCompany(btn.dataset.id);
        showToast("Company deactivated.");
        await loadCompaniesCache();
        await drawCompanies();
      } catch (e) {
        showToast(e.message, true);
      }
    })
  );
}

// ───────────────────────── Company modal (Add/Edit) ─────────────────────────

const companyModal = document.getElementById("company-modal");
const modalOverlay = document.getElementById("modal-overlay");
const companyForm = document.getElementById("company-form");

function openCompanyModal(company) {
  document.getElementById("company-modal-title").textContent = company ? "Edit Company" : "Add Company";
  document.getElementById("company-id-field").value = company ? company.company_id : "";
  document.getElementById("company-name-field").value = company ? company.name : "";
  document.getElementById("company-industry-field").value = company ? (company.industry || "") : "";
  companyModal.classList.add("open");
  modalOverlay.classList.add("open");
}
function closeCompanyModal() {
  companyModal.classList.remove("open");
  modalOverlay.classList.remove("open");
}
document.getElementById("company-modal-close").addEventListener("click", closeCompanyModal);
document.getElementById("company-form-cancel").addEventListener("click", closeCompanyModal);
modalOverlay.addEventListener("click", () => { closeCompanyModal(); closeDetailPanel(); });

companyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("company-id-field").value;
  const data = {
    name: document.getElementById("company-name-field").value.trim(),
    industry: document.getElementById("company-industry-field").value.trim() || null,
  };
  const submitBtn = document.getElementById("company-form-submit");
  submitBtn.disabled = true;
  try {
    if (id) {
      await API.updateCompany(id, data);
      showToast("Company updated.");
    } else {
      await API.createCompany(data);
      showToast("Company created.");
    }
    closeCompanyModal();
    await loadCompaniesCache();
    if (currentPage === "companies") await drawCompanies();
  } catch (e2) {
    showToast(e2.message, true);
  } finally {
    submitBtn.disabled = false;
  }
});

// ───────────────────────── Detail panel (slide-over) ─────────────────────────

const detailPanel = document.getElementById("detail-panel");
const overlay = document.getElementById("overlay");

function openDetailPanel(title, bodyHtml) {
  document.getElementById("detail-panel-title").textContent = title;
  document.getElementById("detail-panel-body").innerHTML = bodyHtml;
  detailPanel.classList.add("open");
  overlay.classList.add("open");
}
function closeDetailPanel() {
  detailPanel.classList.remove("open");
  overlay.classList.remove("open");
}
document.getElementById("detail-panel-close").addEventListener("click", closeDetailPanel);
overlay.addEventListener("click", closeDetailPanel);

// ───────────────────────── Theme toggle ─────────────────────────

const themeBtn = document.getElementById("theme-toggle");
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  themeBtn.textContent = theme === "light" ? "☀ Light" : "☾ Dark";
  localStorage.setItem("dbpilot-theme", theme);
}
themeBtn.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(current === "dark" ? "light" : "dark");
});
applyTheme(localStorage.getItem("dbpilot-theme") || "dark");

// ───────────────────────── Router ─────────────────────────

const PAGES = {
  overview: { title: "Overview", render: renderOverview },
  databases: { title: "Databases", render: renderDatabases },
  "query-performance": { title: "Query Performance", render: renderQueryPerformance },
  alerts: { title: "Alerts", render: renderAlerts },
  policies: { title: "Policies", render: renderPolicies },
  teams: { title: "Teams", render: renderTeams },
  subscriptions: { title: "Subscriptions", render: renderSubscriptions },
  companies: { title: "Company Management", render: renderCompanies },
};

let currentPage = "overview";

function route() {
  const hash = location.hash.replace("#", "") || "overview";
  const page = PAGES[hash] ? hash : "overview";
  currentPage = page;
  pageTitle.textContent = PAGES[page].title;
  navLinks.forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  closeDetailPanel();
  closeCompanyModal();
  PAGES[page].render();
}

window.addEventListener("hashchange", route);

// ───────────────────────── Init ─────────────────────────

(async function init() {
  await checkBackend();
  await loadCompaniesCache();
  route();
  setInterval(checkBackend, 30000);
})();
