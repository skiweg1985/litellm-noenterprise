import { test, expect } from "@playwright/test";
import { openSettings, choose } from "./helpers";
const proxyHTTP = process.env.UI_PROXY_URL!.replace("//proxy:", "//127.0.0.1:");
const adminHeaders = { Authorization: "Bearer sk-identity-test-only" };

test("native SSO form activates complete OIDC policy and preserves it across conflicts and disable", async ({
  page,
  request,
  browser,
}, testInfo) => {
  await openSettings(page);
  await page
    .getByLabel("Issuer URL", { exact: true })
    .fill("http://issuer:8080");
  await page.getByText("Provider advanced options", { exact: true }).click();
  await page
    .getByLabel("Allow HTTP for local testing only", { exact: true })
    .check();
  await page
    .getByRole("button", { name: "Check provider", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Discovery is valid" }),
  ).toBeVisible();
  await page.getByLabel("Application ID", { exact: true }).fill("litellm-test");
  await page.getByLabel("Client secret", { exact: true }).fill("fixture-only");
  await page.getByLabel("Admission attribute", { exact: true }).fill("roles");
  await page
    .getByLabel("Admission allowed values", { exact: true })
    .fill("access");
  await page
    .getByRole("button", { name: "Add role mapping", exact: true })
    .click();
  await page.getByLabel("Mapping 1 attribute", { exact: true }).fill("roles");
  await page
    .getByLabel("Mapping 1 allowed values", { exact: true })
    .fill("operator");
  await choose(page, "Mapping 1 role", "Internal user", "keyboard");
  await page
    .getByRole("button", { name: "Add role mapping", exact: true })
    .click();
  await page.getByLabel("Mapping 2 attribute", { exact: true }).fill("roles");
  await page
    .getByLabel("Mapping 2 allowed values", { exact: true })
    .fill("admin");
  await choose(page, "Mapping 2 role", "Administrator — manage the proxy");
  await page
    .getByRole("button", { name: "Move mapping 2 up", exact: true })
    .focus();
  await page.keyboard.press("Enter");
  await choose(page, "Default role", "Customer");
  await page
    .getByLabel("Allow applications to use provider access tokens", {
      exact: true,
    })
    .check();
  await page.getByLabel("API audience", { exact: true }).fill("litellm-api");
  await page
    .getByLabel("Scope 1 public name", { exact: true })
    .fill("api://litellm-api/access");
  await page.getByLabel("Scope 1 token value", { exact: true }).fill("access");
  await page.getByText("API advanced options", { exact: true }).click();
  await page.getByLabel("Scope attribute", { exact: true }).fill("scp");
  await page.getByLabel("Generic JWT access tokens", { exact: true }).check();
  await page
    .getByRole("button", { name: "Add MCP access rule", exact: true })
    .click();
  await choose(page, "MCP rule 1 server", "managed_a");
  await page.getByLabel("MCP rule 1 attribute", { exact: true }).fill("roles");
  await page
    .getByLabel("MCP rule 1 allowed values", { exact: true })
    .fill("mcp-a");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(
    page.getByText("OpenID Connect is active.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Client secret", { exact: true })).toHaveCount(
    0,
  );
  const saved = await request
    .get(`${proxyHTTP}/oidc/settings`, { headers: adminHeaders })
    .then((r) => r.json());
  expect(
    saved.browser.role_mappings.map((m: { role: string }) => m.role),
  ).toEqual(["proxy_admin", "internal_user"]);
  expect(saved.browser.default_role).toBe("customer");
  expect(saved.browser.mcp_access_rules.managed_a.any_of).toEqual(["mcp-a"]);
  expect(JSON.stringify(saved)).not.toContain("fixture-only");
  const metadata = await request
    .get(`${proxyHTTP}/.well-known/oauth-protected-resource`)
    .then((r) => r.json());
  expect(metadata.scopes_supported).toContain("api://litellm-api/access");
  const validToken = await request
    .post("http://127.0.0.1:8080/control/access-token", { data: {} })
    .then((r) => r.json());
  const providerHeaders = {
    Authorization: `Bearer ${validToken.access_token}`,
  };
  expect(
    (
      await request.get(`${proxyHTTP}/v1/models`, { headers: providerHeaders })
    ).status(),
  ).toBe(200);
  expect(
    (
      await request.get(`${proxyHTTP}/oidc/settings`, {
        headers: providerHeaders,
      })
    ).status(),
  ).toBe(403);
  const wrongScope = await request
    .post("http://127.0.0.1:8080/control/access-token", {
      data: { scp: "other" },
    })
    .then((r) => r.json());
  expect(
    (
      await request.get(`${proxyHTTP}/v1/models`, {
        headers: { Authorization: `Bearer ${wrongScope.access_token}` },
      })
    ).status(),
  ).toBe(403);

  for (const [section, name] of [
    ["OpenID Connect", "top"],
    ["Roles after sign-in", "roles"],
    ["Application and API access", "api"],
  ]) {
    await page
      .getByRole("heading", { name: section, exact: true })
      .scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`oidc-settings-desktop-${name}.png`),
      mask: [page.locator('input[type="password"]')],
    });
  }

  // Invalid edits remain editable; the active policy and credential are retained.
  await page
    .getByLabel("Session lifetime in seconds", { exact: true })
    .fill("45");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Session lifetime" }),
  ).toContainText("Session lifetime");
  await expect(
    page.getByLabel("Session lifetime in seconds", { exact: true }),
  ).toHaveValue("45");
  const unchanged = await request
    .get(`${proxyHTTP}/oidc/settings`, { headers: adminHeaders })
    .then((r) => r.json());
  expect(unchanged.revision).toBe(saved.revision);
  expect(unchanged.browser.session_ttl_seconds).toBe(3600);
  await page
    .getByLabel("Session lifetime in seconds", { exact: true })
    .fill("3600");
  await page
    .getByRole("button", { name: "Replace client secret", exact: true })
    .click();
  await page.getByLabel("Client secret", { exact: true }).fill("fixture-only");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByLabel("Client secret", { exact: true })).toHaveCount(
    0,
  );

  // A committed save can lose its response: the UI must report an unknown outcome.
  await page
    .getByLabel("Session lifetime in seconds", { exact: true })
    .fill("4000");
  await page.route("**/oidc/settings", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    try {
      const forwarded = await route.fetch({
        url: route.request().url().replace("//proxy:", "//127.0.0.1:"),
      });
      expect(forwarded.status()).toBe(200);
    } catch {
      await route.abort("failed");
      throw new Error(
        "The controlled save could not be forwarded to the isolated proxy.",
      );
    }
    await route.abort("connectionreset");
  });
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "could not be confirmed" }),
  ).toContainText("could not be confirmed");
  await expect(
    page.getByLabel("Session lifetime in seconds", { exact: true }),
  ).toHaveValue("4000");
  await page.unroute("**/oidc/settings");
  const afterLostResponse = await request
    .get(`${proxyHTTP}/oidc/settings`, { headers: adminHeaders })
    .then((r) => r.json());
  expect(afterLostResponse.browser.session_ttl_seconds).toBe(4000);
  await page
    .getByRole("button", { name: "Reload saved settings", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Discard edits and reload", exact: true })
    .click();
  await expect(
    page.getByLabel("Session lifetime in seconds", { exact: true }),
  ).toHaveValue("4000");

  // A second administrator changes the same revision through the public API.
  await request.put(`${proxyHTTP}/oidc/settings`, {
    headers: adminHeaders,
    data: {
      expected_revision: afterLostResponse.revision,
      browser: { ...saved.browser, session_ttl_seconds: 7200 },
      api: saved.api,
    },
  });
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "changed elsewhere" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Reload saved settings", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Discard edits and reload", exact: true })
    .click();
  await expect(
    page.getByLabel("Session lifetime in seconds", { exact: true }),
  ).toHaveValue("7200");
  await page
    .getByRole("button", { name: "Disable OpenID Connect", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Confirm disable", exact: true })
    .click();
  await expect(
    page.getByText("OpenID Connect is disabled. Saving enables it.", {
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(
    page.getByText("OpenID Connect is active.", { exact: true }),
  ).toBeVisible();

  // The saved configuration actually controls a fresh browser login.
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: {
      sub: "settings-admin",
      email: "admin@example.test",
      roles: ["access", "admin", "operator"],
    },
  });
  const oidcContext = await browser.newContext();
  const oidcPage = await oidcContext.newPage();
  await oidcPage.goto(`${process.env.UI_PROXY_URL}/oidc/login`);
  await expect(oidcPage).toHaveURL(/\/ui/);
  const admitted = await oidcPage.evaluate(
    async () =>
      (
        await fetch("/oidc/settings", {
          headers: { Authorization: "Bearer oidc-browser-session" },
        })
      ).status,
  );
  expect(admitted).toBe(200);
  await oidcPage.goto(`${process.env.UI_PROXY_URL}/ui/admin-panel/`);
  await oidcPage
    .getByRole("tab", { name: "SSO Settings", exact: true })
    .click();
  await oidcPage
    .getByLabel("Session lifetime in seconds", { exact: true })
    .fill("6000");
  await oidcPage
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(
    oidcPage.getByRole("link", { name: "Sign in again", exact: true }),
  ).toBeVisible();
  await oidcPage
    .getByRole("link", { name: "Sign in again", exact: true })
    .click();
  await expect(oidcPage).toHaveURL(/\/ui/);
  await oidcPage.goto(`${process.env.UI_PROXY_URL}/ui/admin-panel/`);
  await oidcPage
    .getByRole("tab", { name: "SSO Settings", exact: true })
    .click();
  await oidcPage
    .getByRole("button", { name: "Disable OpenID Connect", exact: true })
    .click();
  await oidcPage
    .getByRole("button", { name: "Confirm disable", exact: true })
    .click();
  await expect(
    oidcPage.getByRole("link", {
      name: "Sign in with a native administrator account",
      exact: true,
    }),
  ).toHaveAttribute("href", "/fallback/login");
  await oidcContext.close();
  const disabled = await request
    .get(`${proxyHTTP}/oidc/settings`, { headers: adminHeaders })
    .then((r) => r.json());
  expect(disabled.enabled).toBe(false);
  // Native administrator recovery remains able to re-enable the retained setup.
  expect(
    (
      await request.put(`${proxyHTTP}/oidc/settings`, {
        headers: adminHeaders,
        data: {
          expected_revision: disabled.revision,
          browser: disabled.browser,
          api: disabled.api,
        },
      })
    ).status(),
  ).toBe(200);
});
