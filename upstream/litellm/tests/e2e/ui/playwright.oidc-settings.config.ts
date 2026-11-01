import { defineConfig, devices } from "@playwright/test";
// Automatic failure snapshots can include password-input values. Capture only explicit masked screenshots.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1";
const baseURL = process.env.UI_PROXY_URL;
if (!baseURL)
  throw new Error(
    "UI_PROXY_URL must point to the isolated OIDC settings fixture",
  );
export default defineConfig({
  testDir: "./oidc-settings",
  workers: 1,
  retries: 0,
  timeout: 90000,
  outputDir: process.env.UI_EVIDENCE_DIR ?? "test-results/oidc-settings",
  reporter: "list",
  use: {
    baseURL,
    actionTimeout: 15000,
    channel: process.env.UI_BROWSER_CHANNEL ?? "chromium",
    launchOptions: {
      args: [
        "--host-resolver-rules=MAP proxy 127.0.0.1, MAP issuer 127.0.0.1, MAP mcp-upstream 127.0.0.1",
        "--no-proxy-server",
        `--unsafely-treat-insecure-origin-as-secure=${baseURL}`,
      ],
    },
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  projects: [
    {
      name: "desktop",
      testMatch: "settings.spec.ts",
      use: { viewport: { width: 1440, height: 1000 } },
    },
    {
      name: "phone",
      testMatch: "mobile.spec.ts",
      dependencies: ["desktop"],
      use: { ...devices["Pixel 7"], defaultBrowserType: "chromium" },
    },
  ],
});
