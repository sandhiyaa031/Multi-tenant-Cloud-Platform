// A mechanism test of the canary's automatic rollback, in a real browser.
//
// The change is chosen because it is known to hurt: dropping the index the analytical
// tenant's item lookups depend on. Its queries go back to scanning the whole partition, the
// canary sees its latency leave the contract, and the stored inverse (building the index
// again) runs without anyone asking. This shows that the rollback machinery works; it is not
// a finding about how well DBPilot detects harm.
//   docker compose run --rm e2e npx playwright test rollback.spec.ts
import { expect, test } from "@playwright/test";
import { CRASH, propose, signIn, undoEarlierRuns } from "./helpers";

test.setTimeout(25 * 60_000);

test("a change that breaks the contract in production is rolled back by the canary", async ({ page }) => {
  await signIn(page);
  await undoEarlierRuns(page);
  const current = page.locator(".stepper li[aria-current=step]");

  // Set-up: give the analytical tenant its index and let the canary keep it.
  await propose(page, "Index for one tenant", "canary_only", `e2e rollback setup ${Date.now()}`);
  await expect(current).toContainText("Applied", { timeout: 7 * 60_000 });
  const setup = page.url();
  const applied = await page.locator(".card", { hasText: "Applied to production" }).locator("pre").first().textContent();
  const index = applied?.match(/IF NOT EXISTS (\S+) ON/)?.[1];
  expect(index, "the name of the index that was built").toBeTruthy();

  // The harmful change: drop it again, straight to a canary.
  await page.getByRole("link", { name: "Recommendations", exact: true }).click();
  await page.getByRole("button", { name: "Propose an action" }).click();
  await page.getByLabel("Action").fill(JSON.stringify({ type: "drop_index", index_name: index }));
  await page.getByLabel("Rationale").fill(`e2e rollback drop ${Date.now()}`);
  await page.locator("label.field", { hasText: /^Verification/ }).locator("select").selectOption("canary_only");
  await page.getByRole("button", { name: "Submit for verification" }).click();
  await expect(page).toHaveURL(/\/app\/recommendations\/[0-9a-f-]{36}/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(`Drop index ${index}`);
  await expect(current).toContainText("Canary running", { timeout: 2 * 60_000 });

  // Nobody clicks anything from here: the canary undoes the change by itself.
  await expect(current).toContainText("Rolled back by the canary", { timeout: 8 * 60_000 });
  await expect(page.getByText(/contract breached in 2 of the last 3 windows: t_analytic\/OLAP/).first()).toBeVisible();
  await expect(page.locator(".card", { hasText: "Inverse, used for rollback" }).getByText(/CREATE INDEX/)).toBeVisible();
  await expect(page.getByText(CRASH)).toHaveCount(0);

  // Clean up: the set-up change is still applied (the canary rebuilt its index); roll it back by hand.
  await page.goto(setup);
  await expect(current).toContainText("Applied");
  await page.getByPlaceholder("Reason (recorded in the audit log)").fill("e2e cleanup");
  await page.getByRole("button", { name: "Roll back" }).click();
  await expect(current).toContainText("Rolled back", { timeout: 60_000 });
});
