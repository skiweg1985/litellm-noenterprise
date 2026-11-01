import { test, expect } from "@playwright/test";

test("a newly connected subscription offers reviewed bulk changes with conflicts and retry", async ({
  page,
  request,
}, testInfo) => {
  test.skip(
    process.env.UI_ADMIN_DISABLED === "true",
    "Subscription administration is disabled.",
  );
  await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: "bulk-admin", oid: "bulk-admin", roles: ["access", "admin"] },
  });
  await page.goto("/oidc/login");
  const api = async (path: string, method = "GET", body?: unknown) =>
    page.evaluate(
      async ({ path, method, body }) => {
        const response = await fetch(path, {
          method,
          headers: {
            Authorization: "Bearer oidc-browser-session",
            "Content-Type": "application/json",
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        return { status: response.status, data: await response.json() };
      },
      { path, method, body },
    );
  const suffix = `${testInfo.project.name}-${Date.now()}`;
  const originalIdentity = `bulk-original-${suffix}`;
  const addedIdentity = `bulk-added-${suffix}`;
  const addedName = `Added subscription ${suffix}`;
  const aliases: string[] = [];
  const models: { id: string; name: string; mode: string }[] = [];
  try {
    await request.post("http://127.0.0.1:8091/control", {
      data: {
        identity: originalIdentity,
        catalog_error: false,
        catalog_delay: 0,
        models_by_identity: {},
        status: 200,
        statuses: [],
        status_by_identity: {},
        stream_error: false,
        delay: 0,
        clear: true,
      },
    });
    const first = await api("/chatgpt/api/accounts", "POST", {
      display_name: `Original ${suffix}`,
    });
    expect(first.status).toBe(200);
    aliases.push(first.data.account);
    expect(
      (await api(`/chatgpt/api/accounts/${first.data.account}/login`, "POST"))
        .status,
    ).toBe(200);
    await expect
      .poll(
        async () =>
          (await api("/chatgpt/api/accounts")).data.accounts.find(
            (row: { account: string }) => row.account === first.data.account,
          )?.token_valid,
      )
      .toBe(true);
    for (const mode of ["priority", "distribution", "fixed"]) {
      const name = `bulk-${mode}-${suffix}`;
      const created = await api("/model/new", "POST", {
        model_name: name,
        litellm_params: { model: "chatgpt/gpt-5.4" },
        model_info: {
          subscription_assignment: {
            mode,
            subscription_ids: [first.data.subscription_id],
          },
        },
      });
      expect(created.status).toBe(200);
      models.push({ id: created.data.model_id, name, mode });
    }
    await request.post("http://127.0.0.1:8091/control", {
      data: { identity: addedIdentity },
    });
    await page.goto("/ui/chatgpt-subscriptions/");
    await page
      .getByRole("button", { name: "Add subscription", exact: true })
      .click();
    await page
      .getByRole("textbox", { name: "Subscription name", exact: true })
      .fill(addedName);
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Add subscription", exact: true })
      .click();
    const card = page.getByRole("region", {
      name: `Subscription ${addedName}`,
      exact: true,
    });
    await card
      .getByRole("button", { name: "Start sign-in", exact: true })
      .click();
    await card
      .getByRole("button", { name: "Assign to models", exact: true })
      .click();
    const added = (await api("/chatgpt/api/accounts")).data.accounts.find(
      (row: { display_name: string }) => row.display_name === addedName,
    );
    aliases.push(added.account);
    const dialog = page.getByRole("dialog", {
      name: `Assign ${addedName} to models`,
      exact: true,
    });
    const reserve = dialog.getByRole("region", {
      name: `Model ${models[0].name}`,
      exact: true,
    });
    const distribution = dialog.getByRole("region", {
      name: `Model ${models[1].name}`,
      exact: true,
    });
    const fixed = dialog.getByRole("region", {
      name: `Model ${models[2].name}`,
      exact: true,
    });
    await expect(reserve.getByRole("checkbox")).toBeChecked();
    await expect(distribution.getByRole("checkbox")).toBeChecked();
    await expect(fixed.getByRole("checkbox")).not.toBeChecked();
    await distribution.getByRole("checkbox").uncheck();
    await fixed.getByRole("checkbox").check();
    await fixed.getByRole("combobox").selectOption("replace");
    // A concurrent edit after the review must not be overwritten by this batch.
    expect(
      (
        await api(`/model/${models[0].id}/update`, "PATCH", {
          litellm_params: { temperature: 0.4 },
        })
      ).status,
    ).toBe(200);
    await dialog
      .getByRole("button", { name: "Save selected models", exact: true })
      .click();
    await expect(reserve.getByRole("alert")).toContainText(
      "changed since review",
    );
    await expect(fixed.getByRole("status")).toHaveText("Assignment saved.");
    expect(
      (await api("/chatgpt/api/accounts")).data.accounts.find(
        (row: { account: string }) => row.account === added.account,
      ).token_valid,
    ).toBe(true);
    await dialog
      .getByRole("button", { name: "Refresh review", exact: true })
      .click();
    await expect(fixed).toContainText("already assigned");
    await distribution.getByRole("checkbox").uncheck();
    await dialog
      .getByRole("button", { name: "Save selected models", exact: true })
      .click();
    await expect(reserve.getByRole("status")).toHaveText("Assignment saved.");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      animations: "disabled",
      path: testInfo.outputPath("subscription-bulk-review.png"),
    });
    await dialog.getByRole("button", { name: "Done", exact: true }).click();
    const review = await api(
      `/chatgpt/api/accounts/${added.account}/model-assignments`,
    );
    const membership = (id: string) =>
      review.data.models.find(
        (row: { model_id: string }) => row.model_id === id,
      ).current.subscription_ids;
    expect(membership(models[0].id)).toEqual([
      first.data.subscription_id,
      added.subscription_id,
    ]);
    expect(membership(models[1].id)).toEqual([first.data.subscription_id]);
    expect(membership(models[2].id)).toEqual([added.subscription_id]);
    await request.post("http://127.0.0.1:8091/control", {
      data: { status_by_identity: { [originalIdentity]: 503 }, clear: true },
    });
    expect(
      (
        await api("/v1/responses", "POST", {
          model: models[0].name,
          input: "Use newly assigned reserve",
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await api("/v1/responses", "POST", {
          model: models[2].name,
          input: "Use explicit replacement",
        })
      ).status,
    ).toBe(200);
    expect(
      (await (await request.get("http://127.0.0.1:8091/control")).json()).seen,
    ).toEqual([originalIdentity, addedIdentity, addedIdentity]);
  } finally {
    await request.post("http://127.0.0.1:8091/control", {
      data: { status_by_identity: {}, catalog_error: false, catalog_delay: 0 },
    });
    for (const model of models)
      await api("/model/delete", "POST", { id: model.id });
    for (const alias of aliases)
      await api(`/chatgpt/api/accounts/${alias}/retire`, "POST");
  }
});
