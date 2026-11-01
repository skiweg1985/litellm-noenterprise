import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";

test("subscription logs preserve dispatch attribution and support named filter deep links", async ({ page, request }, testInfo) => {
  test.skip(process.env.UI_ADMIN_DISABLED === "true", "Log administration is deliberately disabled.");
  test.setTimeout(120000);
  const marker = randomUUID();
  const label = `Log seat ${testInfo.project.name} ${marker}`;
  const modelName = `browser-log-${marker}`;
  const identity = `log-seat-${marker}`;
  const actor = await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "logs-browser-admin", oid: "logs-browser-admin", roles: ["access", "admin"] },
  });
  expect(actor.ok()).toBe(true);
  const provider = await request.post("http://127.0.0.1:8091/control", {
    data: { identity, catalog_error: false, status: 200, statuses: [], stream_error: false, delay: 0, clear: true },
  });
  expect(provider.ok()).toBe(true);
  await page.goto("/oidc/login");
  // Use the installed API and real login cookie; no browser routes or log records are mocked.
  const api = async (path: string, method = "GET", body?: unknown) => page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(path, {
      method,
      headers: { Authorization: "Bearer oidc-browser-session", "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, data: await response.json() };
  }, { path, method, body });
  const created = await api("/chatgpt/api/accounts", "POST", { display_name: label });
  expect(created.status).toBe(200);
  const account = created.data;
  let modelId: string | undefined;
  let revision: string | undefined;
  try {
    expect((await api(`/chatgpt/api/accounts/${account.account}/login`, "POST")).status).toBe(200);
    await expect.poll(async () => {
      const result = await api("/chatgpt/api/accounts");
      const connected = result.data.accounts.find((row: { subscription_id: string }) => row.subscription_id === account.subscription_id);
      revision = connected?.connection_revision;
      return connected?.token_valid;
    }).toBe(true);
    expect(revision).toBeTruthy();
    const saved = await api("/model/new", "POST", {
      model_name: modelName,
      litellm_params: { model: "chatgpt/gpt-5.4" },
      model_info: { subscription_assignment: { mode: "fixed", subscription_ids: [account.subscription_id] } },
    });
    expect(saved.status).toBe(200);
    modelId = saved.data.model_id;
    expect(modelId).toBeTruthy();
    const start = new Date(Date.now() - 60000).toISOString();
    const completion = await api("/v1/chat/completions", "POST", {
      model: modelName,
      messages: [{ role: "user", content: `Synthetic log acceptance ${marker}` }],
    });
    expect(completion.status).toBe(200);
    const end = new Date(Date.now() + 60000).toISOString();
    const logQuery = new URLSearchParams({
      start_date: start.slice(0, 19).replace("T", " "),
      end_date: end.slice(0, 19).replace("T", " "),
      subscription_id: account.subscription_id,
      page: "1", page_size: "50",
    });
    let requestId = "";
    await expect.poll(async () => {
      const response = await api(`/spend/logs/ui?${logQuery}`);
      expect(response.status).toBe(200);
      const rows = response.data.data;
      requestId = rows[0]?.request_id ?? "";
      return rows.length;
    }, { timeout: 45000 }).toBe(1);
    const stored = await api(`/spend/logs/ui?${logQuery}`);
    expect(stored.data.data[0].metadata.subscription_attempts).toMatchObject([{
      subscription_id: account.subscription_id, display_name: label, connection_revision: revision,
      outcome: "success", reason: "initial", model: "gpt-5.4",
    }]);
    const observed = await (await request.get("http://127.0.0.1:8091/control")).json();
    expect(observed.seen).toEqual([identity]);

    const deepLink = `/ui/logs/?${new URLSearchParams({ subscription_id: account.subscription_id, start_date: start, end_date: end })}`;
    await page.goto(deepLink);
    const row = page.getByRole("row").filter({ has: page.getByText(label, { exact: true }) });
    await expect(row).toHaveCount(1);
    await expect(page.getByRole("columnheader", { name: "Subscription", exact: true })).toBeVisible();
    await row.getByText(label, { exact: true }).click();
    if (testInfo.project.name === "phone") {
      const viewport = page.viewportSize()!;
      await expect.poll(async () => (await page.getByRole("dialog").boundingBox())?.width ?? 0).toBeGreaterThan(viewport.width * 0.9);
    }
    const attempts = page.getByRole("list", { name: "Subscription attempts", exact: true });
    await expect(attempts.getByRole("listitem")).toHaveCount(1);
    await expect(attempts.getByText(label, { exact: true })).toBeVisible();
    await expect(attempts.getByText(revision!, { exact: true })).toBeVisible();
    await expect(attempts.getByText("Success · Initial attempt", { exact: true })).toBeVisible();
    await expect(attempts.getByText("gpt-5.4", { exact: true })).toBeVisible();

    // A direct detail link starts without a subscription filter. The named action adds it.
    await page.goto(`/ui/logs/?${new URLSearchParams({ log_id: requestId, start_date: start, end_date: end })}`);
    await attempts.getByRole("button", { name: `Filter logs for ${label}`, exact: true }).click();
    await expect(attempts).not.toBeVisible();
    await expect.poll(() => new URL(page.url()).searchParams.get("subscription_id")).toBe(account.subscription_id);
    expect(new URL(page.url()).searchParams.get("log_id")).toBeNull();
    expect(new URL(page.url()).searchParams.get("start_date")).toBe(start);
    expect(new URL(page.url()).searchParams.get("end_date")).toBe(end);
    await page.reload();
    await expect(row).toHaveCount(1);
    await row.getByText(label, { exact: true }).click();
    await expect(attempts.getByText(label, { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ animations: "disabled", path: testInfo.outputPath("subscription-log-attribution.png") });
  } finally {
    if (modelId) expect((await api("/model/delete", "POST", { id: modelId })).status).toBe(200);
    expect((await api(`/chatgpt/api/accounts/${account.account}/retire`, "POST")).status).toBe(200);
  }
});
