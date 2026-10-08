import { defineConfig } from "@playwright/test";

// One browser, one worker: the demo path applies a change to the data plane, and
// only one change may be in canary on a cluster at a time.
export default defineConfig({
  testDir: ".",
  timeout: 10 * 60_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:5173",
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
