import { expect, test } from "@playwright/test";

test("an administrator grants a user access and a factor, and the user opens their usage on a phone", async ({
  page,
  browser,
  request,
  baseURL,
}) => {
  test.skip(
    process.env.UI_ADMIN_DISABLED === "true",
    "Subscription administration is disabled.",
  );
  const id = crypto.randomUUID();
  const email = "fair-use-" + id + "@example.test";
  const name = "Fair use " + id;
  const phone = await browser.newContext({
    baseURL,
    viewport: { width: 390, height: 844 },
  });
  const personal = await phone.newPage();
  const actorUrl = "http://127.0.0.1:8080/control/actor";
  const userActor = { sub: id, oid: id, email, roles: ["access"] };
  const adminActor = {
    sub: "fair-use-admin",
    oid: "fair-use-admin",
    email: "fair-use-admin@example.test",
    roles: ["access", "admin"],
  };
  let account: string | undefined;
  let userId: string | undefined;
  const api = async (path: string, method = "GET", body?: unknown) =>
    page.evaluate(
      async ({ path, method, body }) => {
        const response = await window.fetch(path, {
          method,
          headers: {
            Authorization: "Bearer oidc-browser-session",
            "Content-Type": "application/json",
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        if (!response.ok)
          throw new Error(method + " " + path + " failed: " + response.status);
        return response.json();
      },
      { path, method, body },
    );

  async function removeTestFactor(id: string) {
    const factors = await api("/chatgpt/api/fair-use/factors");
    if (!Object.hasOwn(factors.users, id)) return;
    delete factors.users[id];
    await api("/chatgpt/api/fair-use/factors", "PUT", factors);
  }
  try {
    expect((await request.post(actorUrl, { data: userActor })).ok()).toBe(true);
    await personal.goto("/oidc/login");
    await expect(personal).toHaveURL(/\/ui\/connect\/?$/);
    userId = await personal.evaluate(
      async () =>
        (await (await window.fetch("/oidc/session")).json()).user_id as string,
    );

    expect((await request.post(actorUrl, { data: adminActor })).ok()).toBe(
      true,
    );
    await page.goto("/oidc/login");
    const created = await api("/chatgpt/api/accounts", "POST", {
      display_name: name,
    });
    account = created.account;
    await page.goto("/ui/chatgpt-subscriptions/");
    const card = page.getByRole("region", {
      name: "Subscription " + name,
      exact: true,
    });
    await card.getByRole("button", { name: "Edit access" }).click();
    await page.getByRole("combobox", { name: "Add user" }).fill(email);
    await page.getByRole("option", { name: email, exact: true }).click();
    await page.getByRole("button", { name: "Save access" }).click();
    await expect(card.getByText("1 user", { exact: true })).toBeVisible();
    expect(await api("/chatgpt/api/accounts/" + account + "/access")).toEqual({
      teams: [],
      users: [userId],
    });

    await page.getByRole("link", { name: "Fair use", exact: true }).click();
    const status = page.getByRole("region", { name, exact: true });
    await expect(
      status.getByRole("heading", { name: "5-hour window" }),
    ).toBeVisible();
    await expect(
      status.getByRole("heading", { name: "Weekly window" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Add user factor" }).click();
    await page.getByRole("combobox", { name: "Add user" }).fill(email);
    await page.getByRole("option", { name: email, exact: true }).click();
    await page.getByRole("spinbutton", { name: "Factor" }).fill("2");
    await page.getByRole("button", { name: "Save factor" }).click();
    await expect(
      page
        .getByRole("table", { name: "User factors" })
        .getByRole("row")
        .filter({ hasText: email }),
    ).toContainText("2");

    expect((await api("/chatgpt/api/fair-use/factors")).users[userId!]).toBe(2);
    await personal.getByRole("link", { name: "My subscription usage" }).click();
    await expect(
      personal.getByRole("heading", { name: "My subscription usage" }),
    ).toBeVisible();
    await expect(
      personal.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
    expect(
      await personal.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  } finally {
    await phone.close();
    if (account) {
      try {
        if (userId) await removeTestFactor(userId);
      } finally {
        await api("/chatgpt/api/accounts/" + account + "/retire", "POST");
      }
    }
  }
});
