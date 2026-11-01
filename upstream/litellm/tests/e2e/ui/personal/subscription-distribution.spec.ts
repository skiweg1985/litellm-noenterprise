import { test, expect } from "@playwright/test";

test("an administrator creates and edits a distributed subscription model", async ({ page, request }, testInfo) => {
  test.skip(process.env.UI_ADMIN_DISABLED === "true", "Model administration is disabled.");
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "distribution-admin", oid: "distribution-admin", roles: ["access", "admin"] },
  });
  await page.goto("/oidc/login");
  const api = async (path: string, method = "GET", body?: unknown) => test.step(`${method} ${path}`, () => page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(path, {
      method, headers: { Authorization: "Bearer oidc-browser-session", "Content-Type": "application/json" },
      signal: AbortSignal.timeout(15000),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, data: await response.json() };
  }, { path, method, body }));
  const accounts: { account: string; subscription_id: string; display_name: string; identity: string }[] = [];
  let modelId: string | undefined;
  let journeyFailed = false;
  let releaseSubscriptions = () => {};
  try {
    for (const suffix of ["A", "B"]) {
      const label = `Distribution ${suffix} ${testInfo.project.name}`;
      const identity = `distribution-${suffix}-${testInfo.project.name}`;
      await request.post("http://127.0.0.1:8091/control", {
        data: { identity, catalog_error: false, status: 200, statuses: [], status_by_identity: {}, stream_error: false, delay: 0, clear: true },
      });
      const created = await api("/chatgpt/api/accounts", "POST", { display_name: label });
      expect(created.status).toBe(200);
      const account = { ...created.data, identity };
      accounts.push(account);
      expect((await api(`/chatgpt/api/accounts/${account.account}/login`, "POST")).status).toBe(200);
      await expect.poll(async () => (await api("/chatgpt/api/accounts")).data.accounts
        .find((row: { subscription_id: string }) => row.subscription_id === account.subscription_id)?.token_valid).toBe(true);
    }
    // Exercise a user changing routing before the subscription catalog arrives.
    const subscriptionsReady = new Promise<void>((resolve) => { releaseSubscriptions = resolve; });
    await page.route("**/chatgpt/api/accounts", async (route) => {
      await subscriptionsReady;
      await route.continue();
    });
    await page.goto(`/ui/models-and-endpoints/?tab=add&subscription=${accounts[0].subscription_id}&subscription_model=gpt-5.4`);
    await expect(page.getByRole("table").getByRole("textbox")).toBeVisible();
    await page.getByRole("combobox", { name: "Subscription routing mode", exact: true }).click();
    await page.getByRole("option", { name: "Distribute requests", exact: true }).click();
    await expect(page.getByText(/equal preference/)).toBeVisible();
    await expect(page.getByRole("table").getByRole("textbox")).toBeVisible();
    releaseSubscriptions();
    const picker = page.getByRole("combobox", { name: "Add subscription", exact: true });
    await picker.fill(accounts[1].display_name);
    await page.getByRole("option", { name: accounts[1].display_name, exact: true }).click();
    const modelName = `browser-distribution-${testInfo.project.name}`;
    await page.getByRole("table").getByRole("textbox").fill(modelName);
    const saving = page.waitForResponse((response) => response.url().endsWith("/model/new") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Add Model", exact: true }).click();
    const saved = await saving;
    expect(saved.status()).toBe(200);
    modelId = (await saved.json()).model_id;
    const stored = (await api("/v1/model/info")).data.data.find((row: { model_info: { id: string } }) => row.model_info.id === modelId);
    expect(stored.model_info.subscription_assignment.mode).toBe("distribution");
    expect(stored.model_info.subscription_assignment.subscription_ids).toEqual(accounts.map((account) => account.subscription_id));
    await page.goto(`/ui/models-and-endpoints/?model=${modelId}`);
    await page.getByRole("button", { name: "Edit Settings", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Subscription routing mode", exact: true })).toContainText("Distribute requests");
    await page.getByRole("button", { name: `Remove ${accounts[0].display_name}`, exact: true }).click();
    const updating = page.waitForResponse((response) => response.url().includes(`/model/${modelId}/update`) && response.request().method() === "PATCH");
    await page.getByRole("button", { name: "Save Changes", exact: true }).click();
    expect((await updating).status()).toBe(200);
    await request.post("http://127.0.0.1:8091/control", { data: { clear: true } });
    const response = await api("/v1/chat/completions", "POST", {
      model: modelName, messages: [{ role: "user", content: "Use the remaining assigned account" }],
    });
    expect(response.status).toBe(200);
    expect((await (await request.get("http://127.0.0.1:8091/control")).json()).seen).toEqual([accounts[1].identity]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await test.step("Capture the distributed model on the current viewport", () =>
      page.screenshot({ animations: "disabled", timeout: 10000, path: testInfo.outputPath("subscription-distribution.png") }));
  } catch (error) {
    journeyFailed = true;
    throw error;
  } finally {
    releaseSubscriptions();
    try {
      if (modelId) expect((await api("/model/delete", "POST", { id: modelId })).status).toBe(200);
      for (const account of accounts) expect((await api(`/chatgpt/api/accounts/${account.account}/retire`, "POST")).status).toBe(200);
    } catch (error) {
      if (!journeyFailed) throw error;
      // The enclosing runner destroys the isolated fixture containers after failure.
      // Keep the original journey error when its page is already closed by a timeout.
      console.warn("Subscription fixture cleanup also failed:", String(error));
    }
  }
});
