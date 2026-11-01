import { describe, expect, it } from "vitest";
import { withFixedSubscription } from "./modelAssignment";

describe("fixed subscription assignment", () => {
  it("preserves the assignment revision when changing the selected subscription", () => {
    const info = {
      subscription_assignment: { mode: "fixed", subscription_ids: ["old"], revision: "saved-revision" },
      owner: "example",
    };
    const result = withFixedSubscription({ model: "chatgpt/example" }, info, "new");
    expect(result.info).toEqual({
      owner: "example",
      subscription_assignment: { mode: "fixed", subscription_ids: ["new"], revision: "saved-revision" },
    });
  });
  it("does not invent a revision for a newly created assignment", () => {
    const result = withFixedSubscription({ model: "chatgpt/example" }, {}, "new");
    expect(result.info).toEqual({ subscription_assignment: { mode: "fixed", subscription_ids: ["new"] } });
  });
});
