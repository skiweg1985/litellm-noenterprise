import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within, cleanup, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Page from "./page";
import { ApiError } from "@/lib/http/client";
import type { SubscriptionAccount } from "@/lib/http/chatgptSubscriptions";

const { auth, getSubscriptions, updateSubscription, createSubscription, renameSubscription } = vi.hoisted(() => ({
  auth: {
    accessToken: "oidc-browser-session",
    userId: "test-admin",
    userRole: "proxy_admin",
    isAuthorized: true,
    isLoading: false,
  },
  getSubscriptions: vi.fn(),
  updateSubscription: vi.fn(),
  createSubscription: vi.fn(),
  renameSubscription: vi.fn(),
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => auth }));
vi.mock("@/lib/http/chatgptSubscriptions", () => ({
  getSubscriptions,
  updateSubscription,
  createSubscription,
  renameSubscription,
}));

function account(overrides: Partial<SubscriptionAccount> = {}): SubscriptionAccount {
  return {
    account: "primary",
    subscription_id: "subscription-primary",
    display_name: "primary",
    connection_revision: "revision-primary",
    retired: false,
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
    createSubscription.mockReset().mockResolvedValue(account());
    renameSubscription.mockReset().mockResolvedValue(account());
  });
  afterEach(() => {
    cleanup();
    clients.splice(0).forEach((client) => client.clear());
  });

  it("links an available model to the native form with its subscription preselected", async () => {
    getSubscriptions.mockResolvedValue({
      accounts: [account({ subscription_id: "a28c114f-05bb-4b71-af6f-e114be7c5f36" })],
      problems: [],
    });
    mount();
    expect(await screen.findByRole("link", { name: "Add model example-model" })).toHaveAttribute(
      "href",
      "/ui/models-and-endpoints?tab=add&subscription=a28c114f-05bb-4b71-af6f-e114be7c5f36&subscription_model=example-model",
    );
  });

  it("creates a named subscription and keeps its sign-in an explicit action", async () => {
    const user = userEvent.setup();
    const createdFields = {
      account: "sub_generated",
      display_name: "Development",
      state: "login_required",
      token_valid: false,
    };
    const created = account(createdFields);
    createSubscription.mockImplementation(async () => {
      getSubscriptions.mockResolvedValue({ accounts: [account(), created], problems: [] });
      return created;
    });
    mount();
    await user.click(await screen.findByRole("button", { name: "Add subscription", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "Subscription name" }), { target: { value: "Development" } });
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Add subscription", exact: true }));
    expect(await screen.findByRole("region", { name: "Subscription Development" })).toHaveTextContent(
      "Sign-in required",
    );
    expect(createSubscription).toHaveBeenCalledWith("oidc-browser-session", "Development");
    expect(updateSubscription).not.toHaveBeenCalled();
  });

  it("allows disconnecting expired credentials and lists affected models before confirmation", async () => {
    const user = userEvent.setup();
    getSubscriptions.mockResolvedValue({
      accounts: [account({ state: "token_expired", token_valid: false })],
      problems: [],
    });
    mount();
    await user.click(await screen.findByRole("button", { name: "Disconnect", exact: true }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("region", { name: "Affected models" })).toHaveTextContent("example-model");
    expect(updateSubscription).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Confirm", exact: true }));
    await waitFor(() => expect(updateSubscription).toHaveBeenCalledWith("oidc-browser-session", "primary", "logout"));
  });

  it("renames the display name without changing its account selector", async () => {
    const user = userEvent.setup();
    renameSubscription.mockImplementation(async () => {
      const renamed = account({ display_name: "Production" });
      getSubscriptions.mockResolvedValue({ accounts: [renamed], problems: [] });
      return renamed;
    });
    mount();
    await user.click(await screen.findByRole("button", { name: "Rename", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "Subscription name" }), { target: { value: "Production" } });
    await user.click(screen.getByRole("button", { name: "Save name", exact: true }));
    expect(await screen.findByRole("region", { name: "Subscription Production" })).toHaveTextContent("example-model");
    expect(renameSubscription).toHaveBeenCalledWith("oidc-browser-session", "primary", "Production");
  });

  it("retires only after reviewing affected models and keeps history read-only", async () => {
    const user = userEvent.setup();
    updateSubscription.mockImplementation(async () => {
      const retired = account({ retired: true, state: "retired", token_valid: false });
      getSubscriptions.mockResolvedValue({ accounts: [retired], problems: [] });
      return retired;
    });
    mount();
    await user.click(await screen.findByRole("button", { name: "Retire subscription", exact: true }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("region", { name: "Affected models" })).toHaveTextContent("example-model");
    expect(updateSubscription).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Confirm", exact: true }));
    await user.click(await screen.findByText("Retired subscriptions (1)"));
    const retired = screen.getByRole("region", { name: "Subscription primary" });
    expect(retired).toHaveTextContent("Retired");
    expect(within(retired).queryByRole("button")).not.toBeInTheDocument();
    expect(updateSubscription).toHaveBeenCalledWith("oidc-browser-session", "primary", "retire");
  });

  it("hides account details when saving a name loses administrator access", async () => {
    const user = userEvent.setup();
    createSubscription.mockRejectedValue(new ApiError("denied", 403, {}));
    mount();
    await user.click(await screen.findByRole("button", { name: "Add subscription", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "Subscription name" }), { target: { value: "Development" } });
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Add subscription", exact: true }));
    expect(await screen.findByRole("link", { name: "Sign in again" })).toBeVisible();
    expect(screen.queryByText("current@example.test")).not.toBeInTheDocument();
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
    await user.click(await screen.findByRole("button", { name: "Disconnect", exact: true }));
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
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
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
