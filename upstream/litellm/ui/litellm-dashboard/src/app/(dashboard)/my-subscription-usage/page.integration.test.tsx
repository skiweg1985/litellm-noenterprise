import { afterEach, it, vi, expect } from "vitest";
import { cleanup, renderWithProviders, screen, testQueryClient, within } from "../../../../tests/test-utils";
import Page from "./page";

vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    userId: "alex",
    accessToken: "session",
    userRole: "Internal User",
    isAuthorized: true,
    isLoading: false,
  }),
}));
afterEach(() => {
  cleanup();
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

it("shows an internal user their allowance, local limiting reset and paused subscriptions", async () => {
  const resetFormat: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  const reset = new Date(2026, 9, 1, 14, 30);
  const window = {
    id: "primary",
    share: 48,
    allowance: 40,
    factor: 1,
    throttled: true,
    reset_at: reset.getTime() / 1000,
  };
  const request = vi.fn().mockResolvedValue(
    Response.json({
      user_id: "alex",
      accounts: [
        {
          display_name: "Research",
          enabled: true,
          quota_state: "current",
          windows: [window, { ...window, id: "secondary", share: 8, allowance: 50, throttled: false }],
        },
        { display_name: "Offline", enabled: true, quota_state: "stale", windows: [] },
        { display_name: "Sandbox", enabled: false, quota_state: "current", windows: [] },
      ],
    }),
  );
  vi.stubGlobal("fetch", request);
  renderWithProviders(<Page />);
  expect(await screen.findByRole("heading", { name: "Research" })).toBeVisible();
  const short = within(screen.getByRole("region", { name: "5-hour window" }));
  expect(short.getByText("Your share: 48% · Your allowance: 40%")).toBeVisible();
  expect(short.getByRole("meter")).toHaveAttribute("aria-valuetext", "48% of 40%");
  expect(short.getByText("Limited until " + reset.toLocaleString(undefined, resetFormat))).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Weekly window" })).getByRole("meter")).toHaveAttribute(
    "value",
    "8",
  );
  expect(screen.getByText("Fair use paused – quota data unavailable")).toBeVisible();
  expect(screen.getByText("Fair use is off")).toBeVisible();
  expect(request).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledWith(
    expect.stringMatching(/\/chatgpt\/fair-use\/me$/),
    expect.objectContaining({
      method: "GET",
      credentials: "same-origin",
      headers: expect.objectContaining({ Authorization: "Bearer session" }),
    }),
  );
});
