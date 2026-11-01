import { test, expect } from "@playwright/test";

test("an administrator publishes a subscription model from its catalog and uses that account", async ({ page, request }, testInfo) => {
  test.skip(process.env.UI_ADMIN_DISABLED === "true", "Model administration is deliberately disabled.");
  const label = `Browser subscription ${testInfo.project.name}`;
  const identity = `browser-seat-${testInfo.project.name}`;
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "model-browser-admin", oid: "model-browser-admin", roles: ["access", "admin"] },
  });
  await request.post("http://127.0.0.1:8091/control", { data: { identity, catalog_error: false, status: 200, clear: true } });
  await page.goto("/oidc/login");
  const api = async (path: string, method = "GET", body?: unknown) => page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(path, {
      method, headers: { Authorization: "Bearer oidc-browser-session", "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, data: await response.json() };
  }, { path, method, body });
  const created = await api("/chatgpt/api/accounts", "POST", { display_name: label });
  expect(created.status).toBe(200);
  const account = created.data;
  let modelId: string | undefined;
  try {
    expect((await api(`/chatgpt/api/accounts/${account.account}/login`, "POST")).status).toBe(200);
    await expect.poll(async () => {
      const result = await api("/chatgpt/api/accounts");
      return result.data.accounts.find((row: { subscription_id: string }) => row.subscription_id === account.subscription_id)?.token_valid;
    }).toBe(true);
    await page.goto("/ui/chatgpt-subscriptions/");
    await page.getByRole("region", { name: `Subscription ${label}`, exact: true }).getByRole("link", { name: "Add model gpt-5.4", exact: true }).click();
    await expect(page.getByRole("tab", { name: "Add Model", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("combobox", { name: "ChatGPT subscription", exact: true })).toHaveValue(label);
    const modelName = `browser-subscription-${testInfo.project.name}`;
    await page.getByRole("table").getByRole("textbox").fill(modelName);
    const saved = page.waitForResponse((response) => response.url().endsWith("/model/new") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Add Model", exact: true }).click();
    const result = await saved;
    expect(result.status()).toBe(200);
    modelId = (await result.json()).model_id;
    const completion = await api("/v1/chat/completions", "POST", {
      model: modelName, messages: [{ role: "user", content: "Synthetic browser request" }],
    });
    expect(completion.status).toBe(200);
    const observed = await (await request.get("http://127.0.0.1:8091/control")).json();
    expect(observed.seen).toEqual([identity]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ animations: "disabled", path: testInfo.outputPath("subscription-model.png") });
  } finally {
    if (modelId) await api("/model/delete", "POST", { id: modelId });
    await api(`/chatgpt/api/accounts/${account.account}/retire`, "POST");
  }
});
