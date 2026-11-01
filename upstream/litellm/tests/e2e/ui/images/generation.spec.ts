import { test, expect } from "@playwright/test";

const model = "fixture-openrouter-image";
const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";

test("public image API resolves a named credential and preserves image data and cost", async ({ request }) => {
  const response = await request.post("/v1/images/generations", {
    headers: { Authorization: "Bearer sk-image-fixture-only" },
    data: { model, prompt: "Synthetic API image", size: "1024x1024", quality: "high", n: 1 },
  });
  expect(response.status(), await response.text()).toBe(200);
  const data = await response.json();
  expect(data.data[0].b64_json).toBe(png);
  expect(data.usage).toMatchObject({ input_tokens: 7, output_tokens: 11, total_tokens: 18 });
  expect(Number(response.headers()["x-litellm-response-cost"])).toBe(0.0123);
});

for (const format of ["png", "jpeg", "svg"] as const) {
  test(`Playground displays a decoded ${format} image returned through the native OpenRouter Image API`, async ({ page }) => {
    const login = await page.request.post("/login", {
      form: { username: "admin", password: "fixture-dashboard-only" },
    });
    expect(login.ok()).toBeTruthy();
    await page.goto("/ui/playground/");
    await page.getByPlaceholder("Select an endpoint", { exact: true }).click();
    await page.getByRole("option", { name: "/v1/images/generations", exact: true }).click();
    await page.getByPlaceholder("Select a Model", { exact: true }).click();
    await page.getByRole("option", { name: /^fixture-openrouter-image Mode:/ }).click();
    await expect(page.getByPlaceholder("Select an endpoint", { exact: true })).toHaveValue("/v1/images/generations");
    const response = page.waitForResponse((r) => r.url().endsWith("/images/generations") && r.request().method() === "POST");
    await page.getByPlaceholder("Describe the image you want to generate...", { exact: true }).fill(`Synthetic browser image ${format}`);
    await page.getByPlaceholder("Describe the image you want to generate...", { exact: true }).press("Enter");
    expect((await response).status()).toBe(200);
    const generated = page.getByRole("img", { name: "Generated image", exact: true });
    await expect(generated).toBeVisible();
    const mediaType = format === "svg" ? "svg\\+xml" : format;
    await expect(generated).toHaveAttribute("src", new RegExp(`^data:image/${mediaType};base64,`));
    await expect.poll(() => generated.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth)).toBe(format === "png" ? 1 : 2);
  });
}
