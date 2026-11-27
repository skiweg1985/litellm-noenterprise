import { beforeEach, afterEach, it, expect, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  screen,
  within,
  renderWithProviders,
  testQueryClient,
  waitFor,
} from "../../../../../tests/test-utils";
import userEvent from "@testing-library/user-event";
import Page from "./page";

const resetFormat: Intl.DateTimeFormatOptions = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
const reset = new Date(2026, 9, 1, 14, 30);
const user = { user_id: "u1", share: 45, allowance: 40, factor: 2, throttled: true, tokens: 300, consumption: 12 };
const windowInfo = { threshold: 60, used_percent: 90, scarce: true, reset_at: reset.getTime() / 1000, users: [user] };
const model = {
  model: "preview",
  weight: 1,
  unpriced: true,
  input: null,
  output: null,
  cache_read: null,
  source: "unpriced",
};
const put = vi.fn();
const auth = vi.hoisted(() => ({
  userRole: "Admin",
  userId: "admin",
  accessToken: "session",
  isAuthorized: true,
  isLoading: false,
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => auth }));

beforeEach(() => {
  put.mockClear();
  let factors = { teams: { eng: 1.5 }, users: { u1: 2, removed: 1 } };
  auth.userRole = "Admin";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
      const body = init?.method === "PUT" ? JSON.parse(String(init.body)) : undefined;
      if (body) put(url.pathname, body);
      switch (url.pathname) {
        case "/chatgpt/api/fair-use/status":
          return Response.json({
            accounts: [
              {
                account: "research",
                unknown_ids: { teams: [], users: ["removed"] },
                access: { teams: [], users: [] },
                subscription_id: "one",
                display_name: "Research",
                enabled: true,
                quota_state: "current",
                windows: [
                  { ...windowInfo, id: "primary" },
                  { ...windowInfo, id: "secondary", threshold: 80, users: [], scarce: false, used_percent: 2 },
                ],
              },
              {
                account: "offline",
                unknown_ids: { teams: [], users: ["removed"] },
                access: { teams: [], users: [] },
                subscription_id: "two",
                display_name: "Offline",
                enabled: true,
                quota_state: "unavailable",
                windows: [],
              },
            ],
          });
        case "/chatgpt/api/fair-use/factors":
          if (body) factors = body;
          return Response.json(factors);
        case "/chatgpt/api/fair-use/model-weights":
          return Response.json({
            reference_model: "base",
            overrides: body ?? { other: { factor: 3 } },
            models: [model, { ...model, model: "large", weight: 5, unpriced: false, source: "list" }],
          });
        case "/v2/team/list":
          return Response.json({ teams: [{ team_id: "eng", team_alias: "Engineering" }], total_pages: 1, page: 1 });
        case "/user/list":
          return Response.json({
            users: [{ user_id: "u1", user_email: "alex@example.test" }],
            total_pages: 1,
            page: 1,
          });
        default:
          throw new Error("Unexpected request " + url.pathname);
      }
    }),
  );
});
afterEach(() => {
  cleanup();
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

it("renders both windows, resolved user shares and an unavailable quota", async () => {
  renderWithProviders(<Page />);
  const account = within(await screen.findByRole("region", { name: "Research" }));
  const short = within(account.getByRole("region", { name: "5-hour window" }));
  expect(short.getByText("90% used · limits from 60%")).toBeVisible();
  expect(short.getByText("Limited", { selector: "strong" })).toBeVisible();
  expect(short.getByText("Resets " + reset.toLocaleString(undefined, resetFormat))).toBeVisible();
  expect(await short.findByRole("row", { name: "alex@example.test 45% 40% 2 Limited" })).toBeVisible();
  expect(
    within(account.getByRole("region", { name: "Weekly window" })).getByText("2% used · limits from 80%"),
  ).toBeVisible();
  expect(
    within(screen.getByRole("region", { name: "Offline" })).getByText("Fair use paused – quota data unavailable"),
  ).toBeVisible();
});

it("saves an exempt user factor while preserving team factors", async () => {
  const actor = userEvent.setup();
  renderWithProviders(<Page />);
  await actor.click(await screen.findByRole("button", { name: "Edit factor for alex@example.test" }));
  fireEvent.change(screen.getByRole("spinbutton", { name: "Factor" }), { target: { value: "0" } });
  await actor.click(screen.getByRole("button", { name: "Save factor" }));
  expect(await screen.findByRole("row", { name: /alex@example.test 0 · Exempt/ })).toBeVisible();
  expect(put).toHaveBeenCalledWith("/chatgpt/api/fair-use/factors", {
    teams: { eng: 1.5 },
    users: { u1: 0, removed: 1 },
  });
});

it("saves per-million prices for an unpriced model and preserves other overrides", async () => {
  const actor = userEvent.setup();
  renderWithProviders(<Page />);
  expect(await screen.findByText("No price – counts like base")).toBeVisible();
  expect(screen.getByText("5×")).toBeVisible();
  await actor.click(screen.getByRole("button", { name: "Edit weight for preview" }));
  await actor.click(screen.getByRole("radio", { name: "Set prices" }));
  fireEvent.change(screen.getByRole("spinbutton", { name: "Input price" }), { target: { value: "2" } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Output price" }), { target: { value: "10" } });
  await actor.click(screen.getByRole("button", { name: "Save weight" }));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith("/chatgpt/api/fair-use/model-weights", {
      other: { factor: 3 },
      preview: { input: 0.000002, output: 0.00001 },
    }),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("requires an administrator before reading status or settings", () => {
  auth.userRole = "Internal User";
  renderWithProviders(<Page />);
  expect(screen.getByRole("alert")).toHaveTextContent("Proxy administrator access is required");
  expect(fetch).not.toHaveBeenCalled();
});

it("removes an unknown user factor and keeps other factors intact", async () => {
  const actor = userEvent.setup();
  renderWithProviders(<Page />);
  await actor.click(await screen.findByRole("button", { name: "Edit factor for Unknown user · removed" }));
  await actor.click(screen.getByRole("button", { name: "Remove factor" }));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith("/chatgpt/api/fair-use/factors", { teams: { eng: 1.5 }, users: { u1: 2 } }),
  );
  expect(screen.queryByRole("button", { name: "Edit factor for Unknown user · removed" })).not.toBeInTheDocument();
});
