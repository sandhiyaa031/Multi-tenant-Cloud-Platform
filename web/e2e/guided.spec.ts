// The guided demonstration against the real stack, with nothing mocked.
//
//   1. a real change (the missing index for t_analytic) goes from the rule-based proposer through the
//      canary into production, and then a harmful change (dropping that index) is undone by the canary
//      on its own;
//   2. a verdict the twin really produced earlier is shown as it is: INCONCLUSIVE stays INCONCLUSIVE;
//   3. a viewer sees the same pages and cannot act.
//
// The first test uses the "canary only" verification so that it takes minutes, not hours: the twin
// step is covered by twin.spec.ts and by test 2. It leaves production as it found it.
import { expect, test } from "@playwright/test";
import { CRASH, EMAIL, PASSWORD, get, headers, post, restoreObjective, settle, signIn, undoEarlierRuns } from "./helpers";

const TARGET_ACTION = "Create index on order_line (ol_i_id) for t_analytic";

test("the guided demo: a real change goes into production, and a harmful one is undone by the canary", async ({ page }) => {
  test.setTimeout(30 * 60_000);
  const since = Date.now();
  page.on("dialog", (d) => d.accept());
  await signIn(page);
  await undoEarlierRuns(page);
  try {
    await page.getByRole("link", { name: "Guided demo", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Guided demo", level: 1 })).toBeVisible();
    const step = (n: number) => page.locator(".card", { has: page.getByRole("heading", { name: new RegExp(`^${n}\\. `) }) });

    // ── steps 1-3: signed in, live health, a tenant with a problem ──────────────────
    await expect(step(1)).toContainText(EMAIL);
    await expect(step(2)).toContainText("Tenants");
    await expect(step(3)).toContainText("t_analytic");
    await expect(step(3)).toContainText("WHERE ol_i_id", { ignoreCase: true });            // the statement is read from the live statistics
    await page.getByRole("button", { name: "Show the planner's plan" }).click();
    await expect(step(3).locator("pre").nth(1)).toContainText("Scan");                    // the planner's own plan
    const tighten = page.getByRole("button", { name: /Set objective to p95/ });
    if (await tighten.count()) {
      await tighten.click();
      await expect(page.getByRole("status")).toContainText("objective is now p95 under 50.0 ms");
      const slo = (await get(page, `/clusters/${(await get(page, "/clusters"))[0].id}/slo-status?minutes=15`)).find((s: any) => s.tenant_role === "t_analytic");
      expect(slo.threshold_ms).toBe(50);                                                  // persisted, not only displayed
    }

    // ── step 4: ask the rules, see every candidate, queue one ───────────────────────
    await page.getByLabel("Verification mode").selectOption("canary_only");
    await page.getByRole("button", { name: "Run the rule-based proposer" }).click();
    await expect(page.getByText("What the rules found")).toBeVisible();
    const candidate = page.locator("tr", { hasText: TARGET_ACTION });
    await expect(candidate, "the rules must find the missing index; if not, run scripts/reset_demo.sh").toBeVisible();
    await expect(candidate).toContainText("partition");                                   // the rule's own reason
    await candidate.getByRole("button", { name: "Queue this one" }).click();
    await expect(page.getByRole("status")).toContainText("Queued");
    const queued = (await get(page, "/proposals?limit=100")).filter((p: any) => new Date(p.created_at).getTime() >= since - 5000);
    expect(queued).toHaveLength(1);                                                         // exactly one, not every candidate
    expect(queued[0].source).toBe("rule");
    const chosen = new URL(page.url()).searchParams.get("p")!;
    expect(chosen).toMatch(/^[0-9a-f-]{36}$/);

    // ── steps 5-6: the candidate and why; the engine verifies it ──────────────────────
    await expect(step(5)).toContainText(TARGET_ACTION);
    await expect(step(5)).toContainText("Rule");
    const current = page.locator(".stepper li[aria-current=step]");
    await expect(step(7)).toContainText("twin was skipped");                              // stated, not faked
    await expect(step(8)).toContainText("No gate verdict");

    // ── steps 10-11: the canary applies it in production, and keeps it ──────────────────
    await expect(current).toContainText("Applied", { timeout: 6 * 60_000 });
    await expect(step(10)).toContainText("contract held for every tenant");
    await expect(step(10)).toContainText("t_analytic/OLAP");
    await expect(step(11)).toContainText("Applied");

    // ── step 12: the audit trail and the ledger ─────────────────────────────────────────
    await expect(step(12).locator("td.mono", { hasText: "proposals.insert" })).toBeVisible();
    await expect(step(12).locator("td.mono", { hasText: "proposals.update" }).first()).toBeVisible();
    await expect(step(12)).toContainText("Recorded in the outcome ledger");

    // ── the harmful change ────────────────────────────────────────────────────────────────
    await page.getByRole("button", { name: "Propose dropping it" }).click();
    await expect(page.getByRole("status")).toContainText("Queued with the canary");
    await expect(step(5)).toContainText("Drop index");
    await expect(current).toContainText("Rolled back by the canary", { timeout: 9 * 60_000 });
    await expect(step(11)).toContainText("contract breached in 2 of the last 3 windows");
    await expect(step(10)).toContainText("t_analytic/OLAP");

    // ── clean up: the change the canary kept is rolled back by a person ─────────────────────
    await page.goto(`/app/demo?p=${chosen}`);
    await expect(current).toContainText("Applied");
    await page.getByPlaceholder("Reason (recorded in the audit log)").fill("e2e cleanup");
    await page.getByRole("button", { name: "Roll back" }).click();
    await expect(current).toContainText("Rolled back", { timeout: 2 * 60_000 });
    await expect(page.getByText(CRASH)).toHaveCount(0);
  } finally {
    await restoreObjective(page);
    await settle(page, since);
  }
});

test("a verdict the twin really produced is shown as it is: inconclusive stays inconclusive", async ({ page }) => {
  await signIn(page);
  const cluster = (await get(page, "/clusters")).find((c: any) => c.primary_host);
  let found: any = null;
  for (const p of (await get(page, `/proposals?cluster_id=${cluster.id}&limit=100`)).filter((p: any) => p.verification === "full" && p.state === "INCONCLUSIVE")) {
    const d = await get(page, `/proposals/${p.id}`);
    if (d.twin_runs.at(-1)?.verdict?.decision === "INCONCLUSIVE" && d.action.tenant_role) { found = d; break; }
  }
  test.skip(!found, "no inconclusive twin verdict is stored yet (run twin.spec.ts once)");

  await page.goto(`/app/demo?p=${found.id}`);
  const card = (n: number) => page.locator(".card", { has: page.getByRole("heading", { name: new RegExp(`^${n}\\. `) }) });
  await expect(card(8)).toContainText(/inconclusive/i);                                  // the badge is styled in lowercase
  await expect(card(8)).toContainText("Uncertain is treated as not safe");
  await expect(card(8)).not.toContainText(/approved?/i);
  // The numbers on the page are the API's.
  const target = found.action.tenant_role;
  const effect = Object.entries<any>(found.twin_runs.at(-1).verdict.effects).sort().find(([k]) => k.startsWith(`${target}/`))![1];   // the page sorts the same way
  const shown = effect.ratio < 0.01 ? "<0.01" : effect.ratio >= 100 ? ">100" : effect.ratio.toFixed(2);
  await expect(card(7).locator("tr", { hasText: `${target}/` }).first()).toContainText(`${shown}×`);
  await expect(card(7)).toContainText(String(found.twin_runs.at(-1).verdict.looks));
  // An INCONCLUSIVE proposal waits for a person: the override is offered to an admin, nothing is applied.
  await expect(card(9)).toContainText("Override and approve");
  await expect(card(10)).toContainText("No canary yet");
  await expect(page.getByText(CRASH)).toHaveCount(0);
});

test("a viewer sees the pages and cannot act", async ({ page }) => {
  await signIn(page);
  const email = `viewer-${Date.now()}@example.com`;
  const password = "viewer-password-1234";
  const invited = await post(page, "/members", { email, full_name: "Review Viewer", role: "VIEWER" });
  expect(invited.status()).toBe(201);
  const link: string = (await invited.json()).link;
  expect(link, "with no mail server the invitation returns its single-use link").toContain("token=");
  expect((await page.request.post("/api/v1/auth/reset-password", { data: { token: link.split("token=")[1], new_password: password } })).status()).toBe(200);
  try {
    await page.evaluate(() => localStorage.clear());
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/app\/overview/);

    await page.getByRole("link", { name: "Guided demo", exact: true }).click();
    await expect(page.getByText("Proposing needs the operator role")).toBeVisible();
    await expect(page.getByRole("button", { name: "Run the rule-based proposer" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Override and approve|Approve for canary|Reject|Roll back/ })).toHaveCount(0);
    await expect(page.getByText(/t_analytic runs analytical queries/)).toBeVisible();        // reading is allowed

    await page.getByRole("navigation").getByRole("link", { name: "Organizations", exact: true }).click();
    await expect(page.getByText(/You are a viewer here/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Archive this organization" })).toHaveCount(0);
    await page.getByRole("navigation").getByRole("link", { name: "Settings", exact: true }).click();
    await expect(page.getByRole("button", { name: "Invite" })).toHaveCount(0);

    // And the API refuses what the page does not offer.
    const h = await headers(page);
    const cluster = (await (await page.request.get("/api/v1/clusters", { headers: h })).json())[0];
    expect((await page.request.post(`/api/v1/clusters/${cluster.id}/diagnose`, { headers: h, data: { source: "rule" } })).status()).toBe(403);
    expect((await page.request.post(`/api/v1/clusters/${cluster.id}/diagnose/preview`, { headers: h })).status()).toBe(403);
    expect((await page.request.patch("/api/v1/orgs/current", { headers: h, data: { name: "Hijacked" } })).status()).toBe(403);
    expect((await page.request.post("/api/v1/members", { headers: h, data: { email: "x@example.com", full_name: "X", role: "ADMIN" } })).status()).toBe(403);
  } finally {
    await page.evaluate(() => localStorage.clear());
    await signIn(page);
    const member = (await get(page, "/members")).find((m: any) => m.email === email);
    if (member) await page.request.delete(`/api/v1/members/${member.user_id}`, { headers: await headers(page) });
  }
});

test("a change the planner would not use is rejected for real, and the guided page says where and why", async ({ page }) => {
  await signIn(page);
  const cluster = (await get(page, "/clusters")).find((c: any) => c.primary_host);
  // An index on a column no observed statement filters by: the planner (T1) refuses it in seconds, and
  // production is never touched. This is a real REJECT, produced by the real tier.
  const made = await post(page, `/clusters/${cluster.id}/proposals`, {
    action: { type: "create_index", table: "history", columns: ["h_amount"], tenant_role: "t_steady" }, rationale: "e2e reject probe", verification: "full" });
  expect(made.status()).toBe(201);
  const id = (await made.json()).id;
  await page.goto(`/app/demo?p=${id}`);
  const card = (n: number) => page.locator(".card", { has: page.getByRole("heading", { name: new RegExp(`^${n}\\. `) }) });
  await expect(page.locator(".stepper li[aria-current=step]")).toContainText("Rejected in verification", { timeout: 2 * 60_000 });
  await expect(card(6)).toContainText("the planner would not use this index");
  await expect(card(7)).toContainText("Not run: the planner what-if (T1) refused this proposal first");
  await expect(card(8)).toContainText("never reached the gate");
  await expect(card(9)).toContainText("Nothing is waiting for a decision");
  await expect(card(10)).toContainText("No canary: this proposal never went to production");
  await expect(card(11)).toContainText("Production was never touched");
  await expect(page.getByRole("button", { name: /Override and approve|Approve for canary/ })).toHaveCount(0);
  await expect(card(12).locator("td.mono", { hasText: "proposals.update" }).first()).toBeVisible();
  const stored = await get(page, `/proposals/${id}`);
  expect(stored.state).toBe("REJECTED");
  expect(stored.canary).toBeNull();                                                         // the database agrees: nothing was applied
  expect(stored.twin_runs).toHaveLength(0);
});

// Route A of docs/DEMO_GUIDE.md: the twin's measurement is a stored, real one; the decision, the canary and the rollback are live.
// Opt-in because it consumes one stored INCONCLUSIVE proposal (it ends rolled back): RUN_ROUTE_A=1.
test("route A: a stored twin verdict, then a live human decision, canary and rollback", async ({ page }) => {
  test.skip(!process.env.RUN_ROUTE_A, "opt-in: it consumes one stored inconclusive proposal (run with RUN_ROUTE_A=1)");
  test.setTimeout(20 * 60_000);
  const since = Date.now();
  page.on("dialog", (d) => d.accept());
  await signIn(page);
  await undoEarlierRuns(page);
  const cluster = (await get(page, "/clusters")).find((c: any) => c.primary_host);
  let stored: any = null;
  for (const p of (await get(page, `/proposals?cluster_id=${cluster.id}&limit=100`)).filter((p: any) =>
      p.state === "INCONCLUSIVE" && p.verification === "full" && p.source === "manual" && p.action.type === "create_index" && p.action.tenant_role === "t_analytic")) {
    const d = await get(page, `/proposals/${p.id}`);
    if (d.twin_runs.at(-1)?.verdict?.decision === "INCONCLUSIVE") { stored = d; break; }
  }
  test.skip(!stored, "no stored inconclusive twin verdict for the t_analytic index (run twin.spec.ts once)");
  const card = (n: number) => page.locator(".card", { has: page.getByRole("heading", { name: new RegExp(`^${n}\\. `) }) });
  try {
    await page.goto(`/app/demo?p=${stored.id}`);
    await expect(card(7)).toContainText("Digital twin result");
    await expect(card(8)).toContainText(/inconclusive/i);
    const reason = `reviewed the stored evidence ${since}`;
    await page.getByPlaceholder("Reason (recorded in the audit log)").fill(reason);
    await page.getByRole("button", { name: "Override and approve" }).click();
    await expect(page.locator(".stepper li[aria-current=step]")).toContainText("Applied", { timeout: 6 * 60_000 });
    await expect(card(10)).toContainText("contract held for every tenant");
    await expect(card(12)).toContainText("INCONCLUSIVE → APPROVED");                       // the human decision, in the audit trail
    await expect(card(12)).toContainText(reason);
    await page.getByPlaceholder("Reason (recorded in the audit log)").fill("e2e cleanup");
    await page.getByRole("button", { name: "Roll back" }).click();
    await expect(page.locator(".stepper li[aria-current=step]")).toContainText("Rolled back", { timeout: 2 * 60_000 });
  } finally {
    const now = await get(page, `/proposals/${stored.id}`);
    if (now.state === "APPLIED") await post(page, `/proposals/${stored.id}/rollback`, { reason: "e2e cleanup" });
    await settle(page, since);
  }
});

// Silence unused import warnings for values only some tests use.
void PASSWORD;
