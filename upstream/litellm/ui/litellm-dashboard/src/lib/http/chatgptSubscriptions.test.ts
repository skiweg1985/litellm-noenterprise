import { afterEach, describe, expect, it, vi } from "vitest";
import { getSubscriptions, updateSubscription } from "./chatgptSubscriptions";

vi.mock("@/components/networking", () => ({ getProxyBaseUrl: () => "https://gateway.example.test/proxy" }));

describe("subscription API session transport", () => {
  afterEach(() => vi.unstubAllGlobals());
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
