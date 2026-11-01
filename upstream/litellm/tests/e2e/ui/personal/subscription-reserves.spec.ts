import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

test("administrators create and reorder a preferred subscription with reserves in native model forms", async ({ page, request }, testInfo) => {
  test.skip(process.env.UI_ADMIN_DISABLED === "true", "Model administration is deliberately disabled.");
  test.setTimeout(120000);
  const marker = randomUUID();
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "reserve-browser-admin", oid: "reserve-browser-admin", roles: ["access", "admin"] },
  });
  await page.goto("/oidc/login");
  const api = async (path: string, method = "GET", body?: unknown) => page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(path, { method,
      headers: { Authorization: "Bearer oidc-browser-session", "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, data: await response.json() };
  }, { path, method, body });
  const accounts: { account: string; subscription_id: string; display_name: string }[] = [];
  let modelId: string | undefined;
  try {
    for (const suffix of ["Preferred", "Reserve"]) {
      const label = `${suffix} ${marker}`;
      expect((await request.post("http://127.0.0.1:8091/control", {
        data: { identity: `${suffix.toLowerCase()}-${marker}`, catalog_error: false, status: 200, statuses: [], status_by_identity: {}, models_by_identity: {}, stream_error: false, delay: 0 },
      })).ok()).toBe(true);
      const created = await api("/chatgpt/api/accounts", "POST", { display_name: label });
      expect(created.status).toBe(200);
      accounts.push(created.data);
      expect((await api(`/chatgpt/api/accounts/${created.data.account}/login`, "POST")).status).toBe(200);
      await expect.poll(async () => {
        const overview = await api("/chatgpt/api/accounts");
        return overview.data.accounts.find((row: { subscription_id: string }) => row.subscription_id === created.data.subscription_id)?.token_valid;
      }).toBe(true);
    }
    await page.goto("/ui/chatgpt-subscriptions/");
    await page.getByRole("region", { name: `Subscription ${accounts[0].display_name}`, exact: true }).getByRole("link", { name: "Add model gpt-5.4", exact: true }).click();
    await page.getByRole("combobox", { name: "Subscription routing mode", exact: true }).click();
    await page.getByRole("option", { name: "Preferred subscription with reserves", exact: true }).click();
    await page.getByRole("combobox", { name: "Add subscription", exact: true }).click();
    await page.getByRole("option", { name: accounts[1].display_name, exact: true }).click();
    const modelName = `browser-reserve-${marker}`;
    await page.getByRole("table").getByRole("textbox").fill(modelName);
    const saving = page.waitForResponse((response) => response.url().endsWith("/model/new") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Add Model", exact: true }).click();
    const saved = await saving;
    expect(saved.status()).toBe(200);
    modelId = (await saved.json()).model_id;
    expect(modelId).toBeTruthy();
    const readModel = async () => {
      const response = await api(`/v1/model/info?litellm_model_id=${encodeURIComponent(modelId!)}`);
      expect(response.status).toBe(200);
      expect(response.data.data).toHaveLength(1);
      return response.data.data[0];
    };
    expect((await readModel()).model_info.subscription_assignment).toMatchObject({
      mode: "priority", subscription_ids: accounts.map((account) => account.subscription_id),
    });
    await page.goto(`/ui/models-and-endpoints/?model=${encodeURIComponent(modelId!)}`);
    await page.getByRole("button", { name: "Edit Settings", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Subscription routing mode", exact: true })).toContainText("Preferred subscription with reserves");
    await page.getByRole("button", { name: `Move ${accounts[1].display_name} up`, exact: true }).click();
    const patching = page.waitForResponse((response) => response.url().endsWith(`/model/${modelId}/update`) && response.request().method() === "PATCH");
    await page.getByRole("button", { name: "Save Changes", exact: true }).click();
    expect((await patching).status()).toBe(200);
    expect((await readModel()).model_info.subscription_assignment).toMatchObject({
      mode: "priority", subscription_ids: [accounts[1].subscription_id, accounts[0].subscription_id],
    });
    await page.reload();
    const members = page.getByRole("list", { name: "ChatGPT subscriptions", exact: true }).getByRole("listitem");
    await expect(members).toHaveCount(2);
    await expect(members.nth(0)).toContainText(accounts[1].display_name);
    await expect(members.nth(1)).toContainText(accounts[0].display_name);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ animations: "disabled", path: testInfo.outputPath("subscription-reserve-order.png") });
  } finally {
    if (modelId) expect((await api("/model/delete", "POST", { id: modelId })).status).toBe(200);
    for (const account of accounts) expect((await api(`/chatgpt/api/accounts/${account.account}/retire`, "POST")).status).toBe(200);
  }
});
