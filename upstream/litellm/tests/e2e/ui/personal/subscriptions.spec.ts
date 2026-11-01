import { test, expect } from "@playwright/test";

const path = "/ui/chatgpt-subscriptions/";
const api = "/chatgpt/api/accounts";

test("subscription management admits OIDC administrators and confirms account replacement", async ({ page, request }, testInfo) => {
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "subscription-admin", oid: "subscription-admin", email: "admin@example.test", roles: ["access", "admin"] },
  });
  await page.goto("/oidc/login");
  await page.goto(path);
  if (process.env.UI_ADMIN_DISABLED === "true") {
    await expect(page).toHaveURL(/\/ui\/connect\/?$/);
    return;
  }
  await expect(page.getByRole("heading", { name: "ChatGPT subscriptions", exact: true })).toBeVisible();
  // Exercise the installed backend with the real OIDC cookie before mocking UI-only states.
  const realStatus = await page.evaluate(async () => (await fetch("/chatgpt/api/accounts", { headers: { Authorization: "Bearer oidc-browser-session" } })).status);
  expect(realStatus).toBe(200);

  const account = {
    account: "example-seat", subscription_id: "example-subscription-id", display_name: "example-seat", connection_revision: "example-revision", retired: false, state: "ok", token_valid: true, email: "current@example.test",
    token_expires_at: null, last_refresh_at: null, auto_login: false,
    credentials: ["example-credential"], models: ["example-model"], requests_24h: 3,
    known_account: { email: "current@example.test", account_id: "workspace-current" },
    pending_account: { email: "new@example.test", account_id: "workspace-new" },
    quota: { limits: [{ id: "codex", allowed: true, primary: { remaining_percent: 75, window_seconds: 18000, reset_at: null }, secondary: null }] },
    available_models: { models: [{ slug: "example-model", name: "Example model", configured: true, context_window: null, reasoning: [] }], missing: [] },
  };
  let confirmations = 0;
  await page.route(`**${api}**`, async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: { accounts: [account], problems: [] } });
    else {
      expect(route.request().url()).toContain("/pending/confirm");
      confirmations++;
      await route.fulfill({ status: 503, json: { detail: "Synthetic temporary failure" } });
    }
  });
  await page.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(page.getByRole("region", { name: "Subscription example-seat" })).toBeVisible();
  await expect(page.getByText("75% remaining")).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await page.getByRole("button", { name: "Use new account", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(confirmations).toBe(0);
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  expect(confirmations).toBe(0);
  await page.getByRole("button", { name: "Use new account", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("could not be completed");
  expect(confirmations).toBe(1);
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ animations: "disabled", path: testInfo.outputPath("subscriptions.png") });
});

test("personal OIDC users cannot access subscription accounts", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "subscription-reader", oid: "subscription-reader", roles: ["access", "mcp-a"] },
  });
  await page.goto("/oidc/login");
  await page.goto(path);
  await expect(page).toHaveURL(/\/ui\/connect\/?$/);
  const status = await page.evaluate(async () => (await fetch("/chatgpt/api/accounts", { headers: { Authorization: "Bearer oidc-browser-session" } })).status);
  expect(status).toBe(403);
  await expect(page.getByRole("heading", { name: "ChatGPT subscriptions", exact: true })).toHaveCount(0);
});

test("native administrators can open subscription management with their existing login", async ({ page }) => {
  test.skip(process.env.UI_ADMIN_DISABLED === "true", "The administration UI is deliberately disabled.");
  await page.goto("/ui/login/");
  await page.getByRole("textbox", { name: "Username", exact: true }).fill("admin");
  await page.getByLabel("Password", { exact: true }).fill("sk-identity-test-only");
  await page.getByRole("button", { name: "Login", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/?(?:\?login=success)?$/);
  await page.goto(path);
  await expect(page.getByRole("heading", { name: "ChatGPT subscriptions", exact: true })).toBeVisible();
  await expect(page.getByText(/subscriptions signed in/)).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
});


test("administrators create, rename, disconnect and retire subscriptions without server configuration", async ({ page, request }, testInfo) => {
  test.skip(process.env.UI_ADMIN_DISABLED === "true", "The administration UI is deliberately disabled.");
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "subscription-manager", oid: "subscription-manager", roles: ["access", "admin"] },
  });
  await page.goto("/oidc/login");
  await page.goto(path);
  const name = `Browser subscription ${testInfo.project.name} ${Date.now()}`;
  const renamed = `${name} renamed`;
  await page.getByRole("button", {name: "Add subscription", exact: true}).click();
  await page.getByRole("textbox", {name: "Subscription name", exact: true}).fill(name);
  await page.getByRole("dialog").getByRole("button", {name: "Add subscription", exact: true}).click();
  const account = page.getByRole("region", {name: `Subscription ${name}`, exact: true});
  await expect(account).toBeVisible();
  await expect(account).toContainText("Sign-in required");
  await expect(account.getByRole("button", {name: "Start sign-in", exact: true})).toBeVisible();
  // Reload proves creation persists at the installed API; no provider login is started.
  await page.reload();
  await expect(account).toBeVisible();
  await account.getByRole("button", {name: "Rename", exact: true}).click();
  await page.getByRole("textbox", {name: "Subscription name", exact: true}).fill(renamed);
  await page.getByRole("button", {name: "Save name", exact: true}).click();
  const updated = page.getByRole("region", {name: `Subscription ${renamed}`, exact: true});
  await expect(updated).toBeVisible();
  await updated.getByRole("button", {name: "Disconnect", exact: true}).click();
  await expect(page.getByRole("dialog").getByRole("region", {name: "Affected models"})).toContainText("No models assigned.");
  await page.getByRole("dialog").getByRole("button", {name: "Confirm", exact: true}).click();
  await expect(updated).toContainText("Disconnected");
  await expect(updated.getByRole("button", {name: "Start sign-in", exact: true})).toBeVisible();
  await updated.getByRole("button", {name: "Retire subscription", exact: true}).click();
  await expect(page.getByRole("dialog")).toContainText("Its history remains available.");
  await page.getByRole("dialog").getByRole("button", {name: "Confirm", exact: true}).click();
  await expect(updated).not.toBeVisible();
  await page.getByText(/^Retired subscriptions \(/).click();
  await expect(updated).toBeVisible();
  await expect(updated).toContainText("Retired");
  await expect(updated.getByRole("button")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({animations: "disabled", path: testInfo.outputPath("subscription-lifecycle.png")});
});
