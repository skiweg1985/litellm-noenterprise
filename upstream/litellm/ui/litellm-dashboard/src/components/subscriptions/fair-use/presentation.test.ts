import { describe, it, expect } from "vitest";
import { buildOverrides, formatWeight, overrideDraft, perMillion, perToken, policyNotice } from "./presentation";

describe("subscription weights", () => {
  it.each([
    [5, "5×"],
    [0.05, "0.05×"],
    [0, "0×"],
    [0.00001, "0.00001×"],
  ] as const)("formats %s relative to the reference", (value, label) => expect(formatWeight(value)).toBe(label));
  it("converts prices in both directions", () => {
    expect(perToken(2.5)).toBe(0.0000025);
    expect(perMillion(0.0000002)).toBe(0.2);
  });
  const other = { other: { factor: 0.5 } };
  it("preserves other overrides when changing a model or restoring its list price", () => {
    const current = { ...other, test: { like: "previous" } };
    expect(buildOverrides(current, "test", overrideDraft({ like: " reference " }))).toEqual({
      ...other,
      test: { like: "reference" },
    });
    expect(buildOverrides(current, "test", overrideDraft({ factor: 0 }))).toEqual({ ...other, test: { factor: 0 } });
    expect(buildOverrides(current, "test", overrideDraft(undefined))).toEqual(other);
    expect(current.test).toEqual({ like: "previous" });
  });
  it("round-trips explicit prices, including a free or unspecified cache read", () => {
    const prices = { input: 0.000002, output: 0.00001, cache_read: 0 };
    const draft = overrideDraft(prices);
    expect(draft).toMatchObject({ input: "2", output: "10", cache: "0" });
    expect(buildOverrides(other, "test", draft)).toEqual({ ...other, test: prices });
    expect(buildOverrides(other, "test", { ...draft, cache: "" })).toEqual({
      ...other,
      test: { input: prices.input, output: prices.output },
    });
  });
});

describe("fair-use availability", () => {
  it.each(["stale", "unavailable"] as const)("explains %s quota in user terms", (quota_state) =>
    expect(policyNotice({ enabled: true, quota_state })).toBe("Fair use paused – quota data unavailable"),
  );
  it("distinguishes disabled fair use from current quota", () => {
    expect(policyNotice({ enabled: false, quota_state: "current" })).toBe("Fair use is off");
    expect(policyNotice({ enabled: true, quota_state: "current" })).toBeNull();
  });
});
