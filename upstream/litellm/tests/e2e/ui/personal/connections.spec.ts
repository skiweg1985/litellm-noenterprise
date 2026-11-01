import { test, expect } from "@playwright/test";

test("MCP-only users reach personal connections and stay out of administration", async ({
  page,
  request,
}) => {
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: {
      sub: "browser-alice",
      oid: "browser-alice",
      email: "alice@example.test",
      roles: ["access", "mcp-a", "mcp-delegate", "mcp-setup"],
    },
  });
  await page.goto("/oidc/login");
  await expect(page).toHaveURL(/\/ui\/connect\/?$/);
  await expect(
    page.getByRole("heading", { name: "MCP Servers", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "AI Gateway" })).toHaveCount(0);
  await page.goto("/ui/mcp-servers/");
  await expect(page).toHaveURL(/\/ui\/connect\/?$/);
  const denied = await page.evaluate(
    async () =>
      (
        await fetch("/user/list", {
          headers: { Authorization: "Bearer oidc-browser-session" },
        })
      ).status,
  );
  expect(denied).toBe(403);
});

test("managed connection survives sign-out, reconnects and disconnects without exposing tokens", async ({
  page,
  request,
  context,
}, testInfo) => {
  const actor = `browser-${testInfo.project.name}-${Date.now()}`;
  const identity = {
    sub: actor,
    oid: actor,
    email: "alice@example.test",
    roles: ["access", "mcp-a", "mcp-delegate", "mcp-setup"],
  };
  await request.post("http://127.0.0.1:8080/control/actor", { data: identity });
  await request.post("http://127.0.0.1:8090/control/oauth", {
    data: { actor, expires_in: 3600, refresh: true, fail_refresh: false },
  });
  const leakedResponses: string[] = [];
  const pending: Promise<void>[] = [];
  page.on("response", (response) => {
    if (
      (response.headers()["content-type"] ?? "").includes("application/json")
    ) {
      pending.push(
        response
          .text()
          .then((body) => {
            if (
              /mcp-fixture-(access|refresh)-|opaque-provider-token-not-for-browser/.test(
                body,
              )
            )
              leakedResponses.push(new URL(response.url()).pathname);
          })
          .catch(() => {}),
      );
    }
  });
  await page.goto("/oidc/login");
  const server = page.getByRole("button", {
    name: "Open managed_a",
    exact: true,
  });
  await expect(server).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open managed_b", exact: true }),
  ).toHaveCount(0);
  for (
    let step = 0;
    step < 20 &&
    !(await server.evaluate((element) => element === document.activeElement));
    step++
  ) {
    await page.keyboard.press("Tab");
  }
  await expect(server).toBeFocused();
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/connect/);
  await page
    .getByRole("button", { name: "Open managed_a", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true }),
  ).toBeVisible();
  const token = await request
    .post("http://127.0.0.1:8080/control/access-token", { data: identity })
    .then((r) => r.json());
  const headers = { Authorization: `Bearer ${token.access_token}` };
  const call = () =>
    request.post("http://127.0.0.1:4000/mcp-rest/tools/call", {
      headers,
      data: { server_id: "managed_a", name: "whoami", arguments: {} },
    });
  const used = await call();
  expect(used.status()).toBe(200);
  expect(await used.text()).toContain(actor);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/login/);
  await page.goto("/oidc/login");
  await page
    .getByRole("button", { name: "Open managed_a", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true }),
  ).toBeVisible();
  await request.post("http://127.0.0.1:8090/control/revoke", {
    data: { actor },
  });
  const revoked = await call();
  expect(
    revoked.status() !== 200 || (await revoked.json()).isError,
  ).toBeTruthy();
  await page.reload();
  await page
    .getByRole("button", { name: "Open managed_a", exact: true })
    .click();
  await expect(
    page.getByText("Reconnect needed", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    animations: "disabled",
    path: testInfo.outputPath("reconnect.png"),
  });
  await page.getByRole("button", { name: "Reconnect", exact: true }).click();
  await page
    .getByRole("button", { name: "Open managed_a", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true }),
  ).toBeVisible();
  expect((await call()).status()).toBe(200);
  await page.getByRole("button", { name: "Disconnect", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCSS("opacity", "1");
  await page.screenshot({
    animations: "disabled",
    path: testInfo.outputPath("disconnect.png"),
  });
  await expect(
    page.getByRole("button", { name: "Cancel", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alertdialog")).toBeHidden();
  expect((await call()).status()).toBe(200);
  await page.getByRole("button", { name: "Disconnect", exact: true }).click();
  await context.setOffline(true);
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Disconnect", exact: true })
    .click();
  await expect(page.getByRole("alertdialog").getByRole("alert")).toContainText(
    "Could not disconnect",
  );
  expect((await call()).status()).toBe(200);
  await context.setOffline(false);
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Disconnect", exact: true })
    .click();
  await expect(page.getByText("Not connected", { exact: true })).toBeVisible();
  expect((await call()).status()).not.toBe(200);
  const storage = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
    readableSession: document.cookie.includes("litellm_oidc_session"),
  }));
  expect(storage.local).toEqual([]);
  expect(storage.session).toEqual([]);
  expect(storage.readableSession).toBe(false);
  expect(
    (await context.cookies()).find((c) => c.name === "litellm_oidc_session")
      ?.httpOnly,
  ).toBe(true);
  await Promise.all(pending);
  expect(leakedResponses).toEqual([]);
});

test("client-managed and unconfigured servers show honest guidance and network errors", async ({
  page,
  request,
  context,
}, testInfo) => {
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: {
      sub: "guidance",
      oid: "guidance",
      roles: ["access", "mcp-a", "mcp-delegate", "mcp-setup"],
    },
  });
  await page.goto("/oidc/login");
  await expect(
    page.getByRole("button", {
      name: "Open managed_unconfigured",
      exact: true,
    }),
  ).toBeVisible();
  await page.screenshot({
    animations: "disabled",
    path: testInfo.outputPath("connections.png"),
  });
  await page
    .getByRole("button", { name: "Open managed_unconfigured", exact: true })
    .click();
  await expect(page.getByText("Setup needed", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Connect", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page
    .getByRole("button", { name: "Open delegate", exact: true })
    .click();
  await expect(page.getByText("Client-managed", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/Your client supplies authorization/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /^(Connect|Disconnect)$/ }),
  ).toHaveCount(0);
  await page.screenshot({
    animations: "disabled",
    path: testInfo.outputPath("client-managed.png"),
  });
  expect(
    await page.evaluate(() => document.body.scrollWidth <= innerWidth),
  ).toBe(true);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await context.setOffline(true);
  await page
    .getByRole("button", { name: "Refresh connections", exact: true })
    .click();
  await expect(
    page.getByText(
      "Could not load your connections. Refresh or sign in again.",
    ),
  ).toBeVisible();
  await context.setOffline(false);
  await page
    .getByRole("button", { name: "Refresh connections", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Open managed_a", exact: true }),
  ).toBeVisible();
});

test("missing session and unavailable permissions never leave a usable personal page", async ({
  page,
  request,
  context,
}) => {
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "no-servers", oid: "no-servers", roles: ["access"] },
  });
  await page.goto("/oidc/login");
  await expect(
    page.getByText(
      "No MCP servers are available to you. Contact your administrator if you need access.",
    ),
  ).toBeVisible();
  await context.clearCookies({ name: "litellm_oidc_session" });
  await page.reload();
  await expect(page).toHaveURL(/\/ui\/login/);
});

test("administrators follow the configured administration visibility", async ({
  page,
  request,
}) => {
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: {
      sub: "visibility-admin",
      oid: "visibility-admin",
      roles: ["access", "admin", "mcp-a"],
    },
  });
  await page.goto("/oidc/login");
  if (process.env.UI_ADMIN_DISABLED === "true") {
    await expect(page).toHaveURL(/\/ui\/connect\/?$/);
    await expect(
      page.getByRole("heading", { name: "MCP Servers", exact: true }),
    ).toBeVisible();
    await page.goto("/ui/users/");
    await expect(page).toHaveURL(/\/ui\/connect\/?$/);
  } else {
    await page.goto("/ui/mcp-servers/");
    await expect(
      page.getByRole("heading", { name: "MCP Servers", exact: true }),
    ).toBeVisible();
    await expect(page).toHaveURL(/\/ui\/mcp-servers\/?$/);
    const allowed = await page.evaluate(
      async () =>
        (
          await fetch("/user/list", {
            headers: { Authorization: "Bearer oidc-browser-session" },
          })
        ).status,
    );
    expect(allowed).toBe(200);
  }
});
