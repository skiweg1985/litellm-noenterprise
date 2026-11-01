import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./personal",
  workers: 1,
  retries: 0,
  timeout: 60000,
  outputDir: process.env.UI_EVIDENCE_DIR ?? "test-results/personal",
  reporter: "list",
  use: {
    baseURL: "http://proxy:4000",
    // Full Chromium honors the secure-origin flag required by the HTTP fixtures.
    channel: process.env.UI_BROWSER_CHANNEL ?? "chromium",
    launchOptions: {
      args: [
        "--host-resolver-rules=MAP proxy 127.0.0.1, MAP issuer 127.0.0.1, MAP mcp-upstream 127.0.0.1",
        "--no-proxy-server",
        "--unsafely-treat-insecure-origin-as-secure=http://proxy:4000",
      ],
    },
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    {
      name: "phone",
      use: { ...devices["Pixel 7"], defaultBrowserType: "chromium" },
    },
  ],
});
