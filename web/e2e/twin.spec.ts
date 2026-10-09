// The full verified loop in a real browser: a proposal is measured on the digital twin, the
// pair-based verdict is shown, a person decides, and the change goes through the canary and back.
// Slow (the twin replays several pairs of about two minutes each), so it is run on request:
//   docker compose run --rm e2e npx playwright test twin.spec.ts
import { expect, test } from "@playwright/test";
import { CRASH, propose, signIn, throughCanaryAndBack, undoEarlierRuns } from "./helpers";

test.setTimeout(40 * 60_000);

test("a twin-verified proposal shows its pair-based verdict and can be decided, applied and rolled back", async ({ page }) => {
  await signIn(page);
  await undoEarlierRuns(page);
  await propose(page, "Index for one tenant", "full", `e2e twin ${Date.now()}`);
  const current = page.locator(".stepper li[aria-current=step]");
  await expect(current).toContainText(/Proposed|Verifying/);

  // The twin replays pair after pair until the verdict is firm or its budget is spent.
  await expect(current).toContainText(/Awaiting approval|Inconclusive|Rejected/, { timeout: 25 * 60_000 });
  await expect(page.getByText(CRASH)).toHaveCount(0);

  // The verdict as the pair-based gate produced it: pairs counted, each pair's ratio with the
  // arm that ran first, and an interval (or none) for every tenant and class.
  const twin = page.locator(".card", { hasText: "Digital twin result" });
  await expect(twin.locator(".stat", { hasText: "Replay pairs" }).locator(".value")).toHaveText(/^[1-9]\d*$/);
  await expect(twin.locator("th", { hasText: "Ratio in each pair" })).toBeVisible();
  await expect(twin.locator("tr", { hasText: "t_analytic/OLAP" })).toContainText(/(T→C|C→T)/);
  await expect(twin.locator("svg.forest")).toBeVisible();
  await expect(twin.getByText("Reasons")).toBeVisible();
  await expect(page.getByText("T0 · Static rules")).toBeVisible();
  await expect(page.getByText("T2 · Digital twin")).toBeVisible();

  // Keep what the page looked like with this verdict, for whoever reviews the run.
  await test.info().attach("twin verdict page", { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
  await page.screenshot({ path: "test-results/twin-verdict.png", fullPage: true });

  const state = (await current.textContent()) ?? "";
  test.info().annotations.push({ type: "twin verdict", description: state });
  if (state.includes("Rejected")) return; // refused in verification: production is never touched

  // A person decides. An inconclusive verdict is not applied unless an admin overrides it.
  await page.getByPlaceholder("Reason (recorded in the audit log)").fill("e2e: demo decision");
  await page.getByRole("button", { name: state.includes("Inconclusive") ? "Override and approve" : "Approve for canary" }).click();
  await throughCanaryAndBack(page);

  // The twin's prediction now sits beside what production showed.
  await page.getByRole("link", { name: "Experiments", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Prediction against outcome" })).toBeVisible();
  await expect(page.getByText(CRASH)).toHaveCount(0);
});
