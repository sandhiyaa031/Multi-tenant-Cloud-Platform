// The path a demo walks, in a real browser against the running stack:
// sign in, see live telemetry, propose a change, watch it go through the canary
// into production, roll it back, and lose the session.
//
// Needs the full stack and the demo workload:
//   docker compose up -d && docker compose --profile demo up -d demo-load
//   docker compose run --rm e2e
// The slower path through the digital twin is in twin.spec.ts.
import { expect, test } from "@playwright/test";
import { CRASH, EMAIL, PASSWORD, propose, signIn, throughCanaryAndBack, undoEarlierRuns } from "./helpers";

test.beforeAll(() => {
  expect(EMAIL && PASSWORD, "DEMO_ADMIN_EMAIL and DEMO_ADMIN_PASSWORD must be set").toBeTruthy();
});

test("a signed-out visitor is sent to sign in, and a wrong password is refused with a message", async ({ page }) => {
  await page.goto("/app/workloads");
  await expect(page).toHaveURL(/\/login\?next=%2Fapp%2Fworkloads/);
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill("definitely-not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("incorrect email or password");
  // The right password returns to the page that was asked for.
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/app\/workloads/);
  await expect(page.getByRole("heading", { name: "Workloads" })).toBeVisible();
});

test("an ended session returns to sign in and says why", async ({ page }) => {
  await signIn(page);
  await page.evaluate(() => localStorage.setItem("dbpilot.token", "not.a.valid.token"));
  await page.goto("/app/tenants");
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole("status")).toContainText("Your session has ended");
});

test("every console page renders for the demo organization", async ({ page }) => {
  await signIn(page);
  await expect(page.getByText(/collector \d+ (s|min) ago/)).toBeVisible();
  await expect(page.getByText("No tenant traffic"), "the demo workload must be running (see the top of this file)").toHaveCount(0);
  const pages: [string, string][] = [
    ["Tenants", "Tenants"], ["Workloads", "Workloads"], ["Query Intelligence", "Query Intelligence"], ["Database Health", "Database Health"],
    ["Agent Console", "Agent Console"], ["Recommendations", "Recommendations"], ["Digital Twin Lab", "Digital Twin Lab"],
    ["Verification", "Verification"], ["Canary Deployments", "Canary Deployments"], ["Experiments", "Experiments"],
    ["Audit History", "Audit History"], ["Settings", "Settings"],
  ];
  const paths: Record<string, string> = { Tenants: "tenants", Workloads: "workloads", "Query Intelligence": "queries", "Database Health": "health",
    "Agent Console": "agent", Recommendations: "recommendations", "Digital Twin Lab": "twin", Verification: "verification",
    "Canary Deployments": "canary", Experiments: "experiments", "Audit History": "audit", Settings: "settings" };
  for (const [link, heading] of pages) {
    await page.goto(`/app/${paths[link]}`);
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    await page.waitForLoadState("networkidle");
    await expect(page.getByText(CRASH), `${link} crashed`).toHaveCount(0);
    await expect(page.locator(".page .error"), `${link} shows an error`).toHaveCount(0);
  }
  // The canary's rule replayed over quiet telemetry: the numbers on the page are the API's.
  const token = await page.evaluate(() => localStorage.getItem("dbpilot.token"));
  const headers = { Authorization: `Bearer ${token}` };
  const cluster = (await (await page.request.get("/api/v1/clusters", { headers })).json()).find((c: any) => c.primary_host);
  const noise = await (await page.request.get(`/api/v1/clusters/${cluster.id}/canary-noise?minutes=180`, { headers })).json();
  await page.getByRole("link", { name: "Canary", exact: true }).click();
  const card = page.locator(".card", { hasText: "Canary noise check" });
  if (noise.sequences === 0) await expect(card).toContainText("Not enough quiet telemetry yet");
  else for (const key of Object.keys(noise.keys)) await expect(card.locator("tr", { hasText: key })).toBeVisible();
  await card.screenshot({ path: "test-results/canary-noise.png" });
  // Live telemetry: the four seeded tenants and a drawn latency chart.
  await page.getByRole("link", { name: "Dashboard", exact: true }).click();
  await expect(page.locator(".stat", { hasText: "Tenants" }).locator(".value")).toHaveText("4");
  await expect(page.locator(".recharts-line").first()).toBeVisible();
});

test("a stored twin verdict is displayed with its pairs, intervals and target", async ({ page }) => {
  await signIn(page);
  const token = await page.evaluate(() => localStorage.getItem("dbpilot.token"));
  const headers = { Authorization: `Bearer ${token}` };
  const get = async (path: string) => (await page.request.get(`/api/v1${path}`, { headers })).json();
  const cluster = (await get("/clusters")).find((c: any) => c.primary_host);
  let verified: any = null;
  for (const p of (await get(`/proposals?cluster_id=${cluster.id}&limit=300`)).filter((p: any) => p.verification === "full" && p.action.tenant_role)) {
    const detail = await get(`/proposals/${p.id}`);
    if (detail.twin_runs.at(-1)?.verdict?.treatment_first) { verified = detail; break; }
  }
  test.skip(!verified, "no proposal has been through the pair-based twin yet (run twin.spec.ts once)");

  await page.goto(`/app/recommendations/${verified.id}`);
  const twin = page.locator(".card", { hasText: "Digital twin result" });
  const pairs = verified.twin_runs.at(-1).verdict.looks;
  await expect(twin.locator(".stat", { hasText: "Replay pairs" }).locator(".value")).toHaveText(String(pairs));
  // Every tenant and class the twin measured has a row, the target is marked, and each row lists one ratio per pair.
  const target = `${verified.action.tenant_role}/`;
  for (const [key, effect] of Object.entries<any>(verified.twin_runs.at(-1).verdict.effects)) {
    const row = twin.locator("tr", { hasText: key });
    await expect(row).toBeVisible();
    if (key.startsWith(target)) await expect(row).toContainText("target");
    if (effect.pair_ratios) {
      const listed = ((await row.locator(".pairs").textContent()) ?? "").match(/T→C|C→T/g) ?? [];
      expect(listed, `${key}: one ratio per replay pair`).toHaveLength(Object.keys(effect.pair_ratios).length);
    }
  }
  await expect(twin.locator("svg.forest")).toContainText("◂ target");
  // The planning estimate the API adds: for each key with an interval, pairs that would show it unharmed.
  const planning = verified.twin_runs.at(-1).planning;
  await expect(twin.locator("th", { hasText: "Pairs to show unharmed" })).toBeVisible();
  for (const [key, plan] of Object.entries<any>(planning)) {
    const shown = plan.reachable ? (plan.needed == null ? "> 500" : String(plan.needed)) : "not at this ratio";
    await expect(twin.locator("tr", { hasText: key }), `${key}: pairs needed`).toContainText(shown);
  }
  // The whole record of the proposal can be kept as a file.
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download evidence" }).click()]);
  expect(download.suggestedFilename()).toBe(`dbpilot-proposal-${verified.id}.json`);
  await expect(page.getByText(CRASH)).toHaveCount(0);
  await page.screenshot({ path: "test-results/twin-view.png", fullPage: true });

  // The same verdict in the Digital Twin Lab, and prediction beside outcome under Experiments.
  await page.getByRole("link", { name: "Digital Twin", exact: true }).click();
  await expect(page.locator("svg.forest").first()).toBeVisible();
  await page.goto("/app/experiments");
  await expect(page.getByRole("heading", { name: "Prediction against outcome" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expect(page.getByText(CRASH)).toHaveCount(0);
  await page.screenshot({ path: "test-results/experiments.png", fullPage: true });
});

test("a proposed change goes through the canary into production and is rolled back", async ({ page }) => {
  await signIn(page);
  await undoEarlierRuns(page);
  // Submitting opens the new proposal; without the twin it goes straight to a canary.
  await propose(page, "Index for one tenant", "canary_only", `e2e ${Date.now()}`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Create index on order_line");
  // Live in production and watched window by window, kept when every tenant held, then
  // rolled back on request with the stored inverse.
  await throughCanaryAndBack(page);

  // The decision is in the audit history.
  await page.getByRole("link", { name: "Audit History", exact: true }).click();
  await expect(page.getByText("rolled back").first()).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await page.goto("/app/overview");
  await expect(page).toHaveURL(/\/login/);
});
