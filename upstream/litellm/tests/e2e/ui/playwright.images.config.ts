import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./images",
  workers: 1,
  retries: 0,
  timeout: 60000,
  outputDir: process.env.UI_EVIDENCE_DIR ?? "test-results/images",
  reporter: "list",
  use: {
    baseURL: process.env.IMAGE_PROXY_URL,
    actionTimeout: 15000,
    channel: process.env.UI_BROWSER_CHANNEL ?? "chromium",
    viewport: { width: 1440, height: 1000 },
    trace: "off",
    video: "off",
    screenshot: "only-on-failure",
  },
});
