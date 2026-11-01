import { test, expect } from "@playwright/test";

test("retired subscription usage keeps exact UTC log links and empty periods honest", async ({ page, request }, testInfo) => {
  test.skip(process.env.UI_ADMIN_DISABLED === "true", "Subscription administration is disabled.");
  const id = crypto.randomUUID();
  const label = `Usage ${testInfo.project.name} ${id}`;
  const identity = `usage-${id}`;
  await request.post("http://127.0.0.1:8080/control/actor", { data: { sub: "usage-browser-admin", oid: "usage-browser-admin", roles: ["access", "admin"] } });
  await request.post("http://127.0.0.1:8091/control", { data: { identity, catalog_error: false, status: 200, status_by_identity: {}, statuses: [], stream_error: false, delay: 0, clear: true } });
  await page.goto("/oidc/login");
  const api = async (path: string, method = "GET", body?: unknown) => page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(path, { method, headers: { Authorization: "Bearer oidc-browser-session", "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    return { status: response.status, data: await response.json() };
  }, { path, method, body });
  const created = await api("/chatgpt/api/accounts", "POST", { display_name: label });
  expect(created.status).toBe(200);
  const account = created.data;
  let modelId: string | undefined;
  try {
    expect((await api(`/chatgpt/api/accounts/${account.account}/login`, "POST")).status).toBe(200);
    await expect.poll(async () => (await api("/chatgpt/api/accounts")).data.accounts.find((row: { subscription_id: string }) => row.subscription_id === account.subscription_id)?.token_valid).toBe(true);
    const name = `usage-${id}`;
    const model = await api("/model/new", "POST", { model_name: name, litellm_params: { model: "chatgpt/gpt-5.4" }, model_info: { subscription_assignment: { mode: "fixed", subscription_ids: [account.subscription_id] } } });
    expect(model.status).toBe(200);
    modelId = model.data.model_id;
    expect((await api("/v1/responses", "POST", { model: name, input: "Synthetic observed usage" })).status).toBe(200);
    const start = new Date(Date.now() - 3600000).toISOString().slice(0, 19) + "Z";
    const end = new Date(Date.now() + 60000).toISOString().slice(0, 19) + "Z";
    const query = new URLSearchParams({ start_date: start, end_date: end, subscription_id: account.subscription_id });
    await expect.poll(async () => (await api(`/chatgpt/api/observed-usage?${query}`)).data.subscriptions[0]?.provider_attempts, { timeout: 45000 }).toBe(1);
    const renamed = `${label} renamed`;
    expect((await api(`/chatgpt/api/accounts/${account.account}`, "PATCH", { display_name: renamed })).status).toBe(200);
    expect((await api(`/chatgpt/api/accounts/${account.account}/retire`, "POST")).status).toBe(200);
    await page.goto(`/ui/chatgpt-subscriptions/usage/?${query}`);
    const card = page.getByRole("region", { name: `Usage for ${renamed}`, exact: true });
    await expect(card.getByRole("heading", { name: renamed, exact: true })).toBeVisible();
    await expect(card.getByText("Retired subscription", { exact: true })).toBeVisible();
    await expect(card.getByText("2 recorded", { exact: true })).toHaveCount(2);
    await expect(card.getByText(`Names in retained logs: ${label}`, { exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "History coverage" })).toContainText("Complete historical coverage is unknown");
    await page.getByLabel("From (UTC)", { exact: true }).fill("2040-02-01T00:00:00");
    await page.getByLabel("Through (UTC, inclusive)", { exact: true }).fill("2040-02-02T00:00:00");
    await page.getByRole("button", { name: "Apply period", exact: true }).click();
    await expect(card.getByText("Unknown", { exact: true })).toHaveCount(2);
    await expect(card.getByText("2 recorded", { exact: true })).toHaveCount(0);
    await page.getByLabel("From (UTC)", { exact: true }).fill(start.slice(0, 19));
    await page.getByLabel("Through (UTC, inclusive)", { exact: true }).fill(end.slice(0, 19));
    await page.getByRole("button", { name: "Apply period", exact: true }).click();
    await expect(card.getByText("2 recorded", { exact: true })).toHaveCount(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ animations: "disabled", path: testInfo.outputPath("subscription-usage.png") });
    await card.getByRole("link", { name: `View logs for ${renamed}`, exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("subscription_id")).toBe(account.subscription_id);
    expect(new Date(new URL(page.url()).searchParams.get("start_date")!).toISOString()).toBe(new Date(start).toISOString());
    expect(new Date(new URL(page.url()).searchParams.get("end_date")!).toISOString()).toBe(new Date(end).toISOString());
    const logged = page.getByRole("row").filter({ has: page.getByText(label, { exact: true }) });
    await expect(logged).toHaveCount(1);
    await page.reload();
    await expect(logged).toHaveCount(1);
    await page.goBack();
    await expect(card.getByRole("heading", { name: renamed, exact: true })).toBeVisible();
  } finally {
    if (modelId) expect((await api("/model/delete", "POST", { id: modelId })).status).toBe(200);
    await api(`/chatgpt/api/accounts/${account.account}/retire`, "POST");
  }
});
