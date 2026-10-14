// What the pages show against what the API and the database say, and what happens when input is wrong.
// None of these tests changes anything: invalid input is refused before it is stored.
import { expect, test, type Page } from "@playwright/test";
import { CRASH, signIn } from "./helpers";

async function get(page: Page, path: string) {
  const token = await page.evaluate(() => localStorage.getItem("dbpilot.token"));
  return (await page.request.get(`/api/v1${path}`, { headers: { Authorization: `Bearer ${token}` } })).json();
}
async function observed(page: Page) { return (await get(page, "/clusters")).find((c: any) => c.primary_host); }

test("the overview and the tenant pages show what the API holds", async ({ page }) => {
  await signIn(page);
  const cluster = await observed(page);
  const tenants = await get(page, `/tenants?cluster_id=${cluster.id}`);
  const slo = await get(page, `/clusters/${cluster.id}/slo-status?minutes=15`);

  await expect(page.locator(".stat", { hasText: "Tenants" }).locator(".value")).toHaveText(String(tenants.length));
  await expect(page.locator(".stat", { hasText: "Objectives violated" })).toContainText(`of ${slo.length} objectives`);

  await page.getByRole("link", { name: "Tenants", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Tenants", level: 1 })).toBeVisible();
  for (const t of tenants) await expect(page.locator("tr", { hasText: t.db_role }).first()).toContainText(`${t.warehouse_lo}–${t.warehouse_hi}`);
  await expect(page.locator("tbody tr").first()).toBeVisible();
});

test("selecting a tenant shows that tenant's queries, as the API ranks them", async ({ page }) => {
  await signIn(page);
  const cluster = await observed(page);
  const tenants = await get(page, `/tenants?cluster_id=${cluster.id}`);
  const analytic = tenants.find((t: any) => t.db_role === "t_analytic");
  await page.getByRole("link", { name: "Query Intelligence", exact: true }).click();
  await page.getByLabel("Tenant", { exact: true }).selectOption({ label: analytic.name });

  const api = await get(page, `/clusters/${cluster.id}/top-queries?minutes=15&limit=40&tenant_id=${analytic.id}`);
  expect(api.length, "the demo workload gives this tenant at least one statement").toBeGreaterThan(0);
  await expect(page.locator("tbody tr").first()).toContainText(analytic.name);
  const shown = await page.locator("tbody tr td:first-child").allTextContents();
  expect(shown.every((t) => t.trim() === analytic.name), "no other tenant's statement appears").toBe(true);
  expect(shown.length).toBe(api.length);                                                  // the page asked for the same 40 the API returns
  await expect(page.locator("tbody tr").first().locator(".sql")).toHaveAttribute("title", api[0].query);
  await expect(page.getByText(CRASH)).toHaveCount(0);
});

test("the audit page lists what the API's audit log holds", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: "Audit History", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Audit History", level: 1 })).toBeVisible();
  const api = await get(page, "/audit?limit=50");
  expect(api.length).toBeGreaterThan(0);
  await expect(page.locator("tbody tr").first()).toBeVisible();
  const shown = (await page.locator("tbody tr").allTextContents()).join("\n");
  // The newest entries the API has are on the page (a sign-in made by this very test may be newer than the page's copy).
  for (const entry of api.slice(1, 6)) expect(shown, `audit entry ${entry.id}`).toContain(entry.action);
});

test("wrong input is refused with the API's own message, and nothing is stored", async ({ page }) => {
  await signIn(page);
  const cluster = await observed(page);
  const before = (await get(page, `/tenants?cluster_id=${cluster.id}`)).length;

  // A tenant whose warehouse range overlaps another's.
  await page.getByRole("link", { name: "Tenants", exact: true }).click();
  await page.getByRole("button", { name: "Add tenant" }).click();
  await page.getByLabel("Tenant name").fill("overlap");
  await page.getByLabel("Database role").fill("t_overlap");
  await page.getByLabel("First warehouse").fill("1");
  await page.getByLabel("Last warehouse").fill("2");
  await page.getByRole("button", { name: "Add tenant" }).last().click();
  await expect(page.getByRole("alert")).toContainText(/overlaps/);
  expect((await get(page, `/tenants?cluster_id=${cluster.id}`)).length).toBe(before);

  // A proposal that is not JSON, and one that is outside the action space.
  await page.getByRole("link", { name: "Recommendations", exact: true }).click();
  await page.getByRole("button", { name: "Propose an action" }).click();
  await page.getByLabel("Action").fill("not json");
  await page.getByRole("button", { name: "Submit for verification" }).click();
  await expect(page.getByRole("alert")).toContainText("not valid JSON");
  await page.getByLabel("Action").fill(JSON.stringify({ type: "run_sql", sql: "DROP TABLE ch.orders" }));
  await page.getByRole("button", { name: "Submit for verification" }).click();
  await expect(page.getByRole("alert")).toContainText(/not a valid action/);
  const proposals = await get(page, `/proposals?cluster_id=${cluster.id}&limit=5`);
  expect(JSON.stringify(proposals)).not.toContain("DROP TABLE");

  // An organization name that is too short is stopped before it is sent; one that is only symbols is stopped by the API.
  await page.getByRole("navigation").getByRole("link", { name: "Organizations", exact: true }).click();
  await page.getByLabel("Organization name").fill("!!!???");
  await page.getByRole("button", { name: "Create organization" }).click();
  await expect(page.getByRole("alert")).toContainText(/letters or digits/);
});

test("pages that need a cluster, a tenant or a record say so when there is none", async ({ page }) => {
  await signIn(page);
  await page.goto("/app/does-not-exist");
  await expect(page.getByText("There is no such page")).toBeVisible();
  await page.goto("/app/recommendations/00000000-0000-0000-0000-000000000000");
  await expect(page.getByText(/proposal not found/i)).toBeVisible();
  await expect(page.getByText(CRASH)).toHaveCount(0);
});
