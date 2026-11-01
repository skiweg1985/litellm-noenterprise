import { test, expect } from "@playwright/test";
import { openSettings } from "./helpers";
test("saved settings fit a phone and keep credentials write-only", async ({
  page,
}, testInfo) => {
  await openSettings(page);
  const title = await page
    .getByRole("heading", { name: "OpenID Connect", exact: true })
    .boundingBox();
  const lastTabEdge = await page
    .getByRole("tablist")
    .getByRole("tab")
    .evaluateAll((tabs) =>
      Math.max(...tabs.map((tab) => tab.getBoundingClientRect().bottom)),
    );
  expect(
    title!.y,
    "Wrapped navigation tabs must not overlap the settings heading",
  ).toBeGreaterThanOrEqual(lastTabEdge);

  await expect(
    page.getByText("OpenID Connect is active.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Client secret", { exact: true })).toHaveCount(
    0,
  );
  const form = page.getByRole("region", {
    name: "Independent OpenID Connect settings",
  });
  expect(
    await form.evaluate(
      (element) => element.scrollWidth <= element.clientWidth + 1,
    ),
  ).toBeTruthy();
  for (const [section, name] of [
    ["OpenID Connect", "top"],
    ["Roles after sign-in", "roles"],
    ["Application and API access", "api"],
  ]) {
    await page
      .getByRole("heading", { name: section, exact: true })
      .scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`oidc-settings-phone-${name}.png`),
      mask: [page.locator('input[type="password"]')],
    });
  }
  const storage = await page.evaluate(() =>
    JSON.stringify({
      local: { ...localStorage },
      session: { ...sessionStorage },
    }),
  );
  expect(storage).not.toContain("fixture-only");
});
