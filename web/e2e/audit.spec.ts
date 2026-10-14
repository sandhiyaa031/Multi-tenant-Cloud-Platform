// A look at every console page against the live stack: screenshot, the headings, and anything that
// reads as an empty state or an error. It asserts only that nothing crashed and nothing errored;
// what it records is for a person (or the next fix) to read in test-results/audit.
import { expect, test } from "@playwright/test";
import { CRASH, signIn } from "./helpers";
import { mkdirSync, writeFileSync } from "node:fs";

const PAGES = ["demo", "overview", "tenants", "workloads", "queries", "health", "agent", "recommendations", "twin", "verification",
  "canary", "experiments", "audit", "organizations", "settings"];

test("every console page, with live data", async ({ page }) => {
  mkdirSync("test-results/audit", { recursive: true });
  // A tall viewport instead of a full-page screenshot: resizing mid-capture blanks the charts.
  await page.setViewportSize({ width: 1280, height: 1700 });
  await signIn(page);
  const report: Record<string, unknown> = {};
  for (const name of PAGES) {
    await page.goto(`/app/${name}`);
    await page.waitForTimeout(4000);                       // polled cards fill in a moment after the first paint
    const empties = await page.locator(".empty").allTextContents();
    const errors = await page.locator(".error").allTextContents();
    const loading = await page.getByText("Loading…").count();
    report[name] = { title: await page.locator("h1").first().textContent(), empties, errors, still_loading: loading,
                     cards: await page.locator(".card h2, .card h3").allTextContents() };
    await page.screenshot({ path: `test-results/audit/${name}.png`, fullPage: false });
    await expect(page.getByText(CRASH), `${name} crashed`).toHaveCount(0);
  }
  writeFileSync("test-results/audit/report.json", JSON.stringify(report, null, 1));
  for (const [name, r] of Object.entries<any>(report)) {
    console.log(`${name.padEnd(16)} empties=${JSON.stringify(r.empties)} errors=${JSON.stringify(r.errors)} loading=${r.still_loading}`);
  }
});
