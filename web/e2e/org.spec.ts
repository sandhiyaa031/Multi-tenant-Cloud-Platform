// Organizations through the real stack, in both directions: created in the browser and found through
// the API, created through the API and found in the browser; renamed, switched to, archived. Each
// check that something "persisted" signs in again, so what it sees comes from the database and not
// from anything the first session kept.
import { expect, test, type Page } from "@playwright/test";
import { CRASH, EMAIL, PASSWORD, signIn } from "./helpers";

const stamp = Date.now();
const FROM_UI = `Review UI ${stamp}`;
const FROM_API = `Review API ${stamp}`;
const slugOf = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

async function token(page: Page) { return page.evaluate(() => localStorage.getItem("dbpilot.token")); }
async function apiGet(page: Page, path: string) {
  return (await page.request.get(`/api/v1${path}`, { headers: { Authorization: `Bearer ${await token(page)}` } })).json();
}
// A brand-new session: its own login, nothing carried over from the browser's storage.
async function freshSession(page: Page) {
  const r = await page.request.post("/api/v1/auth/login", { data: { email: EMAIL, password: PASSWORD } });
  expect(r.status()).toBe(200);
  return (await r.json()).access_token as string;
}

test("an organization created in the browser is persisted, and one created through the API appears in the browser", async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await signIn(page);
  await page.locator("header").getByRole("link", { name: "Organizations", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Organizations", level: 1 })).toBeVisible();
  // The explanation of what an organization is, and is not.
  await expect(page.getByText("does not create a cluster, a database role or a")).toBeVisible();

  // ── browser -> database ────────────────────────────────────────────────────
  await page.getByLabel("Organization name").fill(FROM_UI);
  await page.getByRole("button", { name: "Create organization" }).click();
  await expect(page.getByText(/was saved in the control-plane database/)).toBeVisible();
  const row = (name: string) => page.locator("tr", { hasText: name });
  await expect(row(FROM_UI)).toContainText(slugOf(FROM_UI));
  await expect(row(FROM_UI)).toContainText("admin");

  await page.reload();                                                           // refresh loses nothing
  await expect(row(FROM_UI)).toBeVisible();
  const bearer = await freshSession(page);                                       // and the API, from a new login, agrees
  const listed = await (await page.request.get("/api/v1/orgs", { headers: { Authorization: `Bearer ${bearer}` } })).json();
  const mine = listed.find((o: any) => o.name === FROM_UI);
  expect(mine, "the organization created in the browser is in the API's list").toBeTruthy();
  expect(mine).toMatchObject({ slug: slugOf(FROM_UI), role: "ADMIN", members: 1, clusters: 0, tenants: 0 });

  // ── API -> browser ─────────────────────────────────────────────────────────
  const created = await page.request.post("/api/v1/orgs", { headers: { Authorization: `Bearer ${bearer}` }, data: { name: FROM_API } });
  expect(created.status()).toBe(201);
  await page.reload();
  await expect(row(FROM_API)).toBeVisible();
  // The topbar switcher offers it too.
  await expect(page.getByLabel("Organization", { exact: true })).toContainText(FROM_API);

  // ── invalid input and conflicts show the API's answer ───────────────────────
  await page.getByLabel("Organization name").fill(FROM_API);
  await page.getByRole("button", { name: "Create organization" }).click();
  await expect(page.getByRole("alert")).toContainText("already exists");
  await page.getByLabel("Organization name").fill("x");
  await expect(page.getByRole("button", { name: "Create organization" })).toBeDisabled();

  // ── switch into the new organization: it is empty, and says what to do ─────────
  await row(FROM_UI).getByRole("button", { name: "Switch to it" }).click();
  await expect(page).toHaveURL(/\/app\/organizations/);
  await expect(page.getByText(`The current organization: ${FROM_UI}`)).toBeVisible();
  await page.getByRole("link", { name: "Dashboard", exact: true }).click();
  await expect(page.getByText("has no database registered")).toBeVisible();
  await page.locator("header").getByRole("link", { name: "Organizations", exact: true }).click();

  // ── rename: the name changes and the identifier does not ───────────────────────
  const renamed = `${FROM_UI} renamed`;
  await page.getByLabel("New name").fill(renamed);
  await page.getByRole("button", { name: "Rename" }).click();
  await expect(page.getByText(/Renamed to/)).toBeVisible();
  await expect(row(renamed)).toContainText(slugOf(FROM_UI));
  await expect(page.locator(".who")).toContainText(renamed);
  const after = (await (await page.request.get("/api/v1/orgs", { headers: { Authorization: `Bearer ${await freshSession(page)}` } })).json())
    .find((o: any) => o.slug === slugOf(FROM_UI));
  expect(after.name).toBe(renamed);

  // ── the audit trail of this organization recorded what happened ─────────────────
  const audit = await apiGet(page, "/audit?limit=20");
  const actions = audit.map((a: any) => a.action);
  expect(actions).toEqual(expect.arrayContaining(["organizations.create", "organizations.rename"]));

  // ── archive: kept, closed, and the session moves to another organization ───────
  await page.getByRole("button", { name: "Archive this organization" }).click();
  await expect(page).toHaveURL(/\/app\/organizations/);
  await expect(row(renamed)).toHaveCount(0);
  await expect(page.getByText("The current organization: Demo Org")).toBeVisible();
  const stillListed = (await (await page.request.get("/api/v1/orgs", { headers: { Authorization: `Bearer ${await freshSession(page)}` } })).json()).map((o: any) => o.slug);
  expect(stillListed).not.toContain(slugOf(FROM_UI));

  // The demo organization still has its cluster, so the UI refuses to archive it and says why.
  await expect(page.getByRole("button", { name: "Archive this organization" })).toBeDisabled();
  await expect(page.getByText(/It still has 1 cluster/)).toBeVisible();

  // ── clean up the one made through the API ───────────────────────────────────────
  await row(FROM_API).getByRole("button", { name: "Switch to it" }).click();
  await page.getByRole("button", { name: "Archive this organization" }).click();
  await expect(row(FROM_API)).toHaveCount(0);
  await expect(page.getByText(CRASH)).toHaveCount(0);
});

test("a signed-out visitor cannot read organizations, and a bad token is refused", async ({ page }) => {
  expect((await page.request.get("/api/v1/orgs")).status()).toBe(401);
  expect((await page.request.get("/api/v1/orgs", { headers: { Authorization: "Bearer nonsense" } })).status()).toBe(401);
  expect((await page.request.post("/api/v1/orgs", { data: { name: "No session" } })).status()).toBe(401);
});
