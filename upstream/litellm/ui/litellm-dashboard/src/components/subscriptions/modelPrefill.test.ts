import { describe, expect, it } from "vitest";
import { subscriptionModelHref, subscriptionPrefillParsers } from "./modelPrefill";

describe("subscription model links", () => {
  it("opens the native Add Model tab with a stable account and catalog slug", () => {
    expect(subscriptionModelHref("a28c114f-05bb-4b71-af6f-e114be7c5f36", "example-model")).toBe(
      "/ui/models-and-endpoints/?tab=add&subscription=a28c114f-05bb-4b71-af6f-e114be7c5f36&subscription_model=example-model",
    );
  });
  it("rejects malformed prefill identity and non-catalog model values", () => {
    expect(subscriptionPrefillParsers.subscription.parse("not-an-id")).toBeNull();
    expect(subscriptionPrefillParsers.subscription_model.parse("https://example.test/model")).toBeNull();
  });
});
