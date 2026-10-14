import { expect, type Page } from "@playwright/test";

export const EMAIL = process.env.DEMO_ADMIN_EMAIL ?? "";
export const PASSWORD = process.env.DEMO_ADMIN_PASSWORD ?? "";
export const CRASH = "This page could not be displayed";

export async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/app\/overview/);
}

// An earlier run that stopped half-way can leave its change in flight or applied. The same
// action cannot be proposed again while it is, so undo it first.
export async function undoEarlierRuns(page: Page) {
  const token = await page.evaluate(() => localStorage.getItem("dbpilot.token"));
  const headers = { Authorization: `Bearer ${token}` };
  const get = async (path: string) => (await page.request.get(`/api/v1${path}`, { headers })).json();
  const cluster = (await get("/clusters")).find((c: any) => c.primary_host);
  await expect(async () => {
    const mine = (await get(`/proposals?cluster_id=${cluster.id}&limit=300`)).filter((p: any) => p.rationale.startsWith("e2e "));
    for (const p of mine.filter((p: any) => p.state === "APPLIED"))
      await page.request.post(`/api/v1/proposals/${p.id}/rollback`, { headers, data: { reason: "e2e: undoing an earlier run" } });
    expect(mine.filter((p: any) => ["PROPOSED", "VERIFYING", "APPROVED", "CANARY", "APPLIED", "ROLLBACK_REQUESTED"].includes(p.state))).toHaveLength(0);
  }).toPass({ timeout: 20 * 60_000, intervals: [5_000] });
}

// Fills the proposal form from a template and submits; the console then opens the new proposal.
export async function propose(page: Page, template: string, verification: "full" | "canary_only", rationale: string) {
  await page.getByRole("link", { name: "Recommendations", exact: true }).click();
  await page.getByRole("button", { name: "Propose an action" }).click();
  await page.getByLabel("Start from").selectOption({ label: template });
  await page.getByLabel("Rationale").fill(rationale);
  await page.locator("label.field", { hasText: /^Verification/ }).locator("select").selectOption(verification);
  await page.getByRole("button", { name: "Submit for verification" }).click();
  await expect(page).toHaveURL(/\/app\/recommendations\/[0-9a-f-]{36}/);
  await expect(page.getByText(rationale)).toBeVisible();
}

// Watches the canary of the open proposal into production, then rolls it back on request.
export async function throughCanaryAndBack(page: Page) {
  const current = page.locator(".stepper li[aria-current=step]");
  await expect(current).toContainText("Canary running", { timeout: 2 * 60_000 });
  await expect(page.getByText("Watching production")).toBeVisible();
  await expect(page.getByText(/CREATE INDEX/)).toBeVisible();
  await expect(page.getByText(/DROP INDEX/)).toBeVisible();
  await expect(current).toContainText("Applied", { timeout: 5 * 60_000 });
  await expect(page.getByText("contract held for every tenant").first()).toBeVisible();
  await page.getByPlaceholder("Reason (recorded in the audit log)").fill("e2e cleanup");
  await page.getByRole("button", { name: "Roll back" }).click();
  await expect(current).toContainText("Rolled back", { timeout: 60_000 });
  await expect(page.getByText(CRASH)).toHaveCount(0);
}

// ── API access and clean-up shared by the guided-demo tests ───────────────────────
export async function headers(page: Page) {
  return { Authorization: `Bearer ${await page.evaluate(() => localStorage.getItem("dbpilot.token"))}` };
}
export async function get(page: Page, path: string) { return (await page.request.get(`/api/v1${path}`, { headers: await headers(page) })).json(); }
export async function post(page: Page, path: string, data: unknown = {}) { return page.request.post(`/api/v1${path}`, { headers: await headers(page), data }); }

// Whatever this test queued or applied is undone, so the next run starts from the same state.
export async function settle(page: Page, since: number) {
  await expect(async () => {
    const cluster = (await get(page, "/clusters")).find((c: any) => c.primary_host);
    const mine = (await get(page, `/proposals?cluster_id=${cluster.id}&limit=100`)).filter((p: any) => new Date(p.created_at).getTime() >= since - 5000);
    for (const p of mine) {
      if (p.state === "APPLIED") await post(page, `/proposals/${p.id}/rollback`, { reason: "e2e cleanup" });
      if (["AWAITING_APPROVAL", "INCONCLUSIVE"].includes(p.state)) await post(page, `/proposals/${p.id}/reject`, { reason: "e2e cleanup" });
    }
    expect(mine.filter((p: any) => ["PROPOSED", "VERIFYING", "APPROVED", "CANARY", "APPLIED", "ROLLBACK_REQUESTED"].includes(p.state))).toHaveLength(0);
  }).toPass({ timeout: 10 * 60_000, intervals: [5_000] });
}
export async function restoreObjective(page: Page) {
  const tenants = await get(page, "/tenants");
  const target = tenants.find((t: any) => t.db_role === "t_analytic");
  if (target) await page.request.put(`/api/v1/tenants/${target.id}/slos`, { headers: await headers(page), data: { query_class: "OLAP", percentile: 95, threshold_ms: 2000 } });
}
