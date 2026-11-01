import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Page from "./page";
import { ApiError } from "@/lib/http/client";
import type { SubscriptionAccount } from "@/lib/http/chatgptSubscriptions";

const { auth, getSubscriptions, updateSubscription } = vi.hoisted(() => ({
  auth: {
    accessToken: "oidc-browser-session",
    userId: "test-admin",
    userRole: "proxy_admin",
    isAuthorized: true,
    isLoading: false,
  },
  getSubscriptions: vi.fn(),
  updateSubscription: vi.fn(),
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => auth }));
vi.mock("@/lib/http/chatgptSubscriptions", () => ({ getSubscriptions, updateSubscription }));

function account(overrides: Partial<SubscriptionAccount> = {}): SubscriptionAccount {
  return {
    account: "primary",
    state: "ok",
    token_valid: true,
    token_expires_at: null,
    last_refresh_at: null,
    auto_login: false,
    email: "current@example.test",
    credentials: ["subscription-credential"],
    models: ["example-model"],
    requests_24h: 4,
    quota: {
      limits: [
        {
          id: "codex",
          allowed: true,
          primary: { remaining_percent: 75, window_seconds: 18000, reset_at: null },
          secondary: null,
        },
      ],
    },
    available_models: {
      models: [{ slug: "example-model", name: "Example model", configured: true, context_window: null, reasoning: [] }],
      missing: [],
    },
    ...overrides,
  };
}

const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <Page />
    </QueryClientProvider>,
  );
}

describe("ChatGPT subscription management", () => {
  beforeEach(() => {
    auth.userRole = "proxy_admin";
    auth.isAuthorized = true;
    auth.isLoading = false;
    getSubscriptions.mockReset().mockResolvedValue({ accounts: [account()], problems: [] });
    updateSubscription.mockReset().mockResolvedValue(account());
  });
  afterEach(() => {
    cleanup();
    clients.splice(0).forEach((client) => client.clear());
  });

  it("uses the existing session and shows quotas and assigned models without a key form", async () => {
    mount();
    expect(await screen.findByRole("region", { name: "Subscription primary" })).toBeVisible();
    expect(screen.getByText("75% remaining")).toBeVisible();
    expect(screen.getByText("Configured", { exact: true })).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(getSubscriptions).toHaveBeenCalledWith("oidc-browser-session", expect.any(AbortSignal));
  });

  it.each(["internal_user", "proxy_admin_viewer", "org_admin"])("does not fetch accounts for %s", (role) => {
    auth.userRole = role;
    mount();
    expect(screen.getByRole("alert")).toHaveTextContent("Proxy administrator access is required");
    expect(getSubscriptions).not.toHaveBeenCalled();
  });

  it("does not fetch before the server session has been checked", () => {
    auth.isLoading = true;
    mount();
    expect(screen.getByRole("status")).toHaveTextContent("Checking your session");
    expect(getSubscriptions).not.toHaveBeenCalled();
  });

  it("requires explicit confirmation before replacing an account", async () => {
    const user = userEvent.setup();
    const pending = account({
      known_account: { email: "current@example.test", account_id: "workspace-one" },
      pending_account: { email: "new@example.test", account_id: "workspace-two" },
    });
    getSubscriptions.mockResolvedValue({ accounts: [pending], problems: [] });
    mount();
    await user.click(await screen.findByRole("button", { name: "Use new account" }));
    expect(updateSubscription).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("new@example.test");
    await user.click(within(dialog).getByRole("button", { name: "Cancel", exact: true }));
    expect(updateSubscription).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Use new account" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirm", exact: true }));
    await waitFor(() =>
      expect(updateSubscription).toHaveBeenCalledWith("oidc-browser-session", "primary", "pending/confirm"),
    );
  });

  it("keeps a failed sign-out visible and does not claim success", async () => {
    const user = userEvent.setup();
    updateSubscription.mockRejectedValue(new ApiError("unavailable", 503, {}));
    mount();
    await user.click(await screen.findByRole("button", { name: "Sign out", exact: true }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirm", exact: true }));
    expect(
      await screen.findByText("The action could not be completed. Close this dialog and refresh the status."),
    ).toBeVisible();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel", exact: true }));
    expect(screen.getByRole("region", { name: "Subscription primary" })).toHaveTextContent("Signed in");
  });

  it("hides cached account details after permission is lost", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByText("current@example.test");
    getSubscriptions.mockRejectedValue(new ApiError("denied", 403, {}));
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(await screen.findByRole("link", { name: "Sign in again" })).toBeVisible();
    expect(screen.queryByText("current@example.test")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("cancels a pending sign-in before requesting another code", async () => {
    const user = userEvent.setup();
    getSubscriptions.mockResolvedValue({
      accounts: [
        account({
          state: "login_pending",
          login: {
            user_code: "EXAMPLE-CODE",
            verification_url: "https://provider.example.test/device",
            expires_at: 2000000000,
          },
        }),
      ],
      problems: [],
    });
    mount();
    expect(await screen.findByRole("link", { name: "Open sign-in page" })).toHaveAttribute(
      "href",
      "https://provider.example.test/device",
    );
    await user.click(screen.getByRole("button", { name: "Request new code" }));
    await waitFor(() => expect(updateSubscription).toHaveBeenCalledTimes(2));
    expect(updateSubscription.mock.calls.map((args) => args[2])).toEqual(["login/cancel", "login"]);
  });

  it("does not create an executable link from an invalid provider URL", async () => {
    getSubscriptions.mockResolvedValue({
      accounts: [
        account({
          state: "login_pending",
          login: { user_code: "EXAMPLE-CODE", verification_url: "javascript:alert(1)", expires_at: 2000000000 },
        }),
      ],
      problems: [],
    });
    mount();
    expect(await screen.findByText("The provider did not return a secure sign-in URL.")).toBeVisible();
    expect(screen.queryByRole("link", { name: "Open sign-in page" })).not.toBeInTheDocument();
  });
});
