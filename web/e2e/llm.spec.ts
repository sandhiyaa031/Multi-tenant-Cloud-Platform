// The LLM proposer, live, from the guided page. Opt-in: it calls a real model provider (the one the server is
// configured with), takes minutes, and uses that provider's quota, so it only runs with RUN_LLM=1:
//
//     docker compose run --rm -e RUN_LLM=1 e2e npx playwright test llm.spec.ts
//
// What it proves is that the proposal on screen came from a live provider: the provider and model recorded
// with it are the ones the server reports, the tool calls are the model's own, and the proposal enters the
// same verification as any other. It does not judge the quality of what the model proposed.
import { expect, test } from "@playwright/test";
import { CRASH, get, restoreObjective, settle, signIn, undoEarlierRuns } from "./helpers";

test.skip(!process.env.RUN_LLM, "opt-in: calls a real model provider; run with RUN_LLM=1");

test("the LLM proposer runs live from the guided page and its provenance is recorded", async ({ page }) => {
  test.setTimeout(30 * 60_000);
  const since = Date.now();
  page.on("dialog", (d) => d.accept());
  await signIn(page);
  await undoEarlierRuns(page);
  const status = await get(page, "/agent/status");
  test.skip(!status.configured, "no LLM provider key is configured on this server");
  try {
    await page.getByRole("link", { name: "Guided demo", exact: true }).click();
    await expect(page.getByText(`LLM provider: ${status.provider}, model ${status.model}`)).toBeVisible();
    // Canary-only so that it ends in minutes; the change, if any, is rolled back below.
    await page.getByLabel("Verification mode").selectOption("canary_only");
    await page.getByRole("button", { name: "Run the LLM agent" }).click();
    await expect(page.getByRole("button", { name: "The LLM agent is investigating…" })).toBeVisible();
    // Either the agent answered, or the provider could not be used (overloaded, out of quota). The second is the provider's
    // state and not DBPilot's: it is reported with the provider's own message and the test is skipped, not failed.
    const outcome = page.getByRole("status").or(page.getByRole("alert")).first();
    await expect(outcome).toContainText(/The LLM agent proposed one action|found nothing to propose|model API/, { timeout: 10 * 60_000 });
    const said = (await outcome.textContent()) ?? "";
    test.skip(/model API/.test(said), `the provider could not be used: ${said}`);

    const id = new URL(page.url()).searchParams.get("p")!;
    const stored = await get(page, `/proposals/${id}`);
    expect(stored.source).toBe("agent");
    expect(stored.evidence.provider).toBe(status.provider);
    expect(stored.evidence.model).toBe(status.model);
    expect(stored.evidence.trace.length, "the model looked at the system before proposing").toBeGreaterThan(2);
    expect(stored.evidence.trace.some((t: any) => t.name === "propose_action")).toBe(true);
    expect(stored.evidence.usage.requests).toBeGreaterThan(1);
    await expect(page.locator(".card", { has: page.getByRole("heading", { name: /^5\. / }) })).toContainText(`${stored.evidence.model}`);
    await expect(page.locator(".card", { has: page.getByRole("heading", { name: /^5\. / }) })).toContainText(`${stored.evidence.trace.length} tool calls`);
    await expect(page.getByText(CRASH)).toHaveCount(0);
  } finally {
    await restoreObjective(page);
    await settle(page, since);
  }
});
