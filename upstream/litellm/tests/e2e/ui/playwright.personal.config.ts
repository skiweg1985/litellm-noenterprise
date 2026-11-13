import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.UI_PROXY_URL ?? "http://proxy:4000";

export default defineConfig({
  testDir: "./personal",
  workers: 1,
  retries: 0,
  timeout: 60000,
  outputDir: process.env.UI_EVIDENCE_DIR ?? "test-results/personal",
  reporter: "list",
  use: {
    baseURL,
    // Full Chromium honors the secure-origin flag required by the HTTP fixtures.
    channel: process.env.UI_BROWSER_CHANNEL ?? "chromium",
    launchOptions: {
      args: [
        "--host-resolver-rules=MAP proxy 127.0.0.1, MAP issuer 127.0.0.1, MAP mcp-upstream 127.0.0.1",
        "--no-proxy-server",
        `--unsafely-treat-insecure-origin-as-secure=${baseURL}`,
      ],
    },
    // Bound every action so a stuck step fails with its own error instead of the test timeout.
    actionTimeout: 15000,
    navigationTimeout: 30000,
    trace: "retain-on-failure",
    video: "off",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    {
      name: "phone",
      use: { ...devices["Pixel 7"], defaultBrowserType: "chromium" },
    },
  ],
});
