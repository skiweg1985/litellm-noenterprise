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
  const banner = page.getByRole("button", {
    name: "Dismiss banner",
    exact: true,
  });
  if (await banner.isVisible()) await banner.click();
}
export async function choose(
  page: Page,
  label: string,
  option: string,
  interaction: "pointer" | "keyboard" = "pointer",
) {
  const control = page.getByRole("combobox", { name: label, exact: true });
  await control.click();
  const target = page.getByRole("option", { name: option, exact: true });
  await expect(target).toBeVisible();
  if (interaction === "keyboard") {
    const names = await page.getByRole("option").allTextContents();
    const index = names.findIndex((name) => name.trim() === option);
    expect(
      index,
      `Option ${option} is offered by ${label}`,
    ).toBeGreaterThanOrEqual(0);
    await page.keyboard.press("Home");
    for (let step = 0; step < index; step++)
      await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
  } else {
    // The portal is already visible. A pointer click avoids Playwright scrolling
    // the portal's zero-height wrapper, which can reposition the open menu.
    const bounds = await target.boundingBox();
    expect(bounds).not.toBeNull();
    const viewport = page.viewportSize()!;
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
    await page.mouse.click(
      bounds!.x + bounds!.width / 2,
      bounds!.y + bounds!.height / 2,
    );
  }
  await expect(control).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(control).toBeFocused();
  await expect(control).toContainText(option);
}
