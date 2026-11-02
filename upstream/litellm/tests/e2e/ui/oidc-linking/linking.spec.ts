import { test, expect } from "@playwright/test";
import { openSettings } from "../oidc-settings/helpers";

const baseURL = process.env.UI_PROXY_URL!;
const proxyHTTP = baseURL.replace("//proxy:", "//127.0.0.1:");
const adminHeaders = { Authorization: "Bearer sk-identity-test-only" };

test("administrator reviews, links and unlinks a verified identity without replacing its existing account", async ({
  page,
  request,
  browser,
}, testInfo) => {
  const subject = "linking-browser-person";
  const userId = "existing-linking-browser-user";
  const email = "linking-person@example.test";
  const created = await request.post(proxyHTTP + "/user/new", {
    headers: adminHeaders,
    data: {
      user_id: userId,
      user_email: email,
      user_role: "internal_user",
      max_budget: 23,
      auto_create_key: false,
    },
  });
  expect(created.ok()).toBeTruthy();
  const actor = await request.post("http://127.0.0.1:8080/control/actor", {
    data: { sub: subject, oid: subject, email, roles: ["access"] },
  });
  expect(actor.ok()).toBeTruthy();

  const member = await browser.newContext();
  const memberPage = await member.newPage();
  try {
    await memberPage.goto(baseURL + "/oidc/login");
    await expect(
      memberPage.getByText("Account linking requires administrator review.", {
        exact: true,
      }),
    ).toBeVisible();
    expect(
      await memberPage.evaluate(
        async () => (await fetch("/oidc/session")).status,
      ),
    ).toBe(401);

    await openSettings(page);
    await page.goto("/ui/users/");
    await page
      .getByRole("tab", { name: "OIDC Account Links", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "OIDC account links", exact: true }),
    ).toBeVisible();
    await page.getByLabel("Find identity or user").fill(subject);
    const card = page.getByRole("region", { name: `Identity ${subject}` });
    await expect(
      card.getByText("Review pending", { exact: true }),
    ).toBeVisible();
    await expect(card.getByText(userId, { exact: false })).toBeVisible();
    await expect(
      card.getByRole("button", { name: "Review link", exact: true }),
    ).toBeDisabled();

    await page.setViewportSize({ width: 393, height: 851 });
    await expect(
      page.getByRole("tab", { name: "OIDC Account Links", exact: true }),
    ).toBeVisible();
    const tabBounds = await page
      .getByRole("tab", { name: "OIDC Account Links", exact: true })
      .boundingBox();
    const headingBounds = await page
      .getByRole("heading", { name: "OIDC account links", exact: true })
      .boundingBox();
    expect(tabBounds).not.toBeNull();
    expect(headingBounds).not.toBeNull();
    expect(headingBounds!.y).toBeGreaterThan(tabBounds!.y + tabBounds!.height);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath("oidc-linking-phone.png"),
      fullPage: true,
    });
    await page.setViewportSize({ width: 1440, height: 1000 });

    const select = card.getByRole("combobox", {
      name: `Existing user account for ${subject}`,
    });
    await select.fill(userId);
    await select.press("ArrowDown");
    await select.press("Enter");
    await expect(
      card.getByText("Current role:", { exact: true }),
    ).toBeVisible();
    await expect(
      card.getByText("OIDC role preview:", { exact: true }),
    ).toBeVisible();

    // A second administrator makes this review stale through the public API.
    const inventory = await request.get(proxyHTTP + "/oidc/links", {
      headers: adminHeaders,
    });
    const link = (await inventory.json()).links.find(
      (value: { subject: string }) => value.subject === subject,
    );
    const rejected = await request.post(
      proxyHTTP + `/oidc/links/${link.identity_key}/reject`,
      {
        headers: adminHeaders,
        data: { expected_revision: link.revision },
      },
    );
    expect(rejected.ok()).toBeTruthy();
    await card
      .getByRole("button", { name: "Review link", exact: true })
      .click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("next fresh sign-in");
    await dialog
      .getByRole("button", { name: "Confirm link", exact: true })
      .click();
    await expect(
      page
        .getByRole("region", { name: "OIDC account links", exact: true })
        .getByRole("alert"),
    ).toContainText("changed elsewhere");
    await page
      .getByRole("button", { name: "Reload links", exact: true })
      .click();
    await expect(
      card.getByText("Request rejected", { exact: true }),
    ).toBeVisible();
    await card
      .getByRole("button", { name: "Reopen review", exact: true })
      .click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Reopen review", exact: true })
      .click();
    await expect(
      card.getByText("Review pending", { exact: true }),
    ).toBeVisible();

    const freshSelect = card.getByRole("combobox", {
      name: `Existing user account for ${subject}`,
    });
    await freshSelect.fill(userId);
    await page.getByRole("option", { name: new RegExp(userId) }).click();
    await card
      .getByRole("button", { name: "Review link", exact: true })
      .click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Confirm link", exact: true })
      .click();
    await expect(
      page.getByRole("status").filter({ hasText: "Account linked." }),
    ).toBeVisible();
    await expect(
      card.getByText("Linked to OIDC", { exact: true }),
    ).toBeVisible();
    expect(
      await memberPage.evaluate(
        async () => (await fetch("/oidc/session")).status,
      ),
    ).toBe(401);

    await memberPage.goto(baseURL + "/oidc/login");
    await expect(memberPage).toHaveURL(/\/ui\/connect\//);
    const session = await memberPage.evaluate(async () =>
      (await fetch("/oidc/session")).json(),
    );
    expect(session.user_id).toBe(userId);
    expect(session.role).toBe("internal_user_viewer");
    await page.screenshot({
      path: testInfo.outputPath("oidc-linking-desktop.png"),
      fullPage: true,
    });

    await card
      .getByRole("button", { name: "Unlink OIDC", exact: true })
      .click();
    await expect(page.getByRole("alertdialog")).toContainText(
      "not a full account suspension",
    );
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Unlink OIDC", exact: true })
      .click();
    await expect(card.getByText("Unlinked", { exact: true })).toBeVisible();
    expect(
      await memberPage.evaluate(
        async () => (await fetch("/oidc/session")).status,
      ),
    ).toBe(401);
    const retained = await request.get(proxyHTTP + "/user/info", {
      headers: adminHeaders,
      params: { user_id: userId },
    });
    expect(retained.ok()).toBeTruthy();
    expect((await retained.json()).user_info).toMatchObject({
      user_id: userId,
      max_budget: 23,
    });
  } finally {
    await member.close();
  }
});
