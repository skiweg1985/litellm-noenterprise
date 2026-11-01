import { test, expect } from "@playwright/test";
import { openSettings } from "./helpers";
test("saved settings fit a phone and keep credentials write-only", async ({
  page,
}, testInfo) => {
  await openSettings(page);
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
  await page.screenshot({
    path: testInfo.outputPath("oidc-settings-phone.png"),
    fullPage: true,
  });
  const storage = await page.evaluate(() =>
    JSON.stringify({
      local: { ...localStorage },
      session: { ...sessionStorage },
    }),
  );
  expect(storage).not.toContain("fixture-only");
});
