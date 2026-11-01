import { expect, type Page } from "@playwright/test";
export async function openSettings(page: Page) {
  await page.goto("/fallback/login");
  await page.getByLabel(/^Username/).fill("admin");
  await page.getByLabel(/^Password/).fill("fixture-settings-admin-only");
  await page.getByRole("button", { name: "Login", exact: true }).click();
  await expect(page).toHaveURL(/\/ui/);
  await page.goto("/ui/admin-panel/");
  await page.getByRole("tab", { name: "SSO Settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "OpenID Connect", exact: true }),
  ).toBeVisible();
}
export async function choose(page: Page, label: string, option: string) {
  await page.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}
