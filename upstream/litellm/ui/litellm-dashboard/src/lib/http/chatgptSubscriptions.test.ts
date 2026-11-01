import { afterEach, describe, expect, it, vi } from "vitest";
import { getSubscriptions, updateSubscription, createSubscription, renameSubscription } from "./chatgptSubscriptions";

vi.mock("@/components/networking", () => ({ getProxyBaseUrl: () => "https://gateway.example.test/proxy" }));

describe("subscription API session transport", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("creates and renames named subscriptions using the same administrator session", async () => {
    const fetch = vi.fn().mockImplementation(
      async () =>
        new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    await createSubscription("oidc-browser-session", "Development");
    await renameSubscription("oidc-browser-session", "stable-alias", "Production");
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "https://gateway.example.test/proxy/chatgpt/api/accounts",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify({ display_name: "Development" }),
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "https://gateway.example.test/proxy/chatgpt/api/accounts/stable-alias",
      expect.objectContaining({
        method: "PATCH",
        credentials: "same-origin",
        body: JSON.stringify({ display_name: "Production" }),
      }),
    );
  });
  it("uses the dashboard bearer marker and same-origin cookies for reads and mutations", async () => {
    const fetch = vi
      .fn()
      .mockImplementation(
        async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
      );
    vi.stubGlobal("fetch", fetch);
    await getSubscriptions("oidc-browser-session");
    await updateSubscription("oidc-browser-session", "example seat", "pending/discard");
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "https://gateway.example.test/proxy/chatgpt/api/accounts",
      expect.objectContaining({
        credentials: "same-origin",
        method: "GET",
        headers: expect.objectContaining({ Authorization: "Bearer oidc-browser-session" }),
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "https://gateway.example.test/proxy/chatgpt/api/accounts/example%20seat/pending/discard",
      expect.objectContaining({ credentials: "same-origin", method: "POST" }),
    );
  });
});
