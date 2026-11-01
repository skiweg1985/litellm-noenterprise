import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, testQueryClient, within } from "../../../tests/test-utils";
import { getSubscriptions } from "@/lib/http/chatgptSubscriptions";
import { SubscriptionAssignmentEditor, type SubscriptionSelection } from "./SubscriptionAssignmentEditor";

vi.mock("@/lib/http/chatgptSubscriptions", () => ({ getSubscriptions: vi.fn() }));
const account = (id: string, name: string, slug = "example") => ({
  account: id, subscription_id: id, display_name: name, token_valid: true, retired: false,
  available_models: { models: [{ slug, name: slug }], missing: [] },
});
function Editor() {
  const [value, setValue] = useState<SubscriptionSelection>({ mode: "fixed", subscription_ids: ["first"] });
  return <SubscriptionAssignmentEditor accessToken="fixture" value={value} onChange={setValue} models={["chatgpt/example"]} />;
}

describe("subscription assignment editor", () => {
  beforeEach(() => {
    testQueryClient.clear();
    vi.mocked(getSubscriptions).mockResolvedValue({
      accounts: [account("first", "Primary"), account("second", "Reserve"), account("wrong", "Other model", "different")], problems: [],
    } as never);
  });
  it("adds only compatible reserves and lets the administrator order and remove them", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Editor />);
    await user.click(await screen.findByRole("combobox", { name: "Subscription routing" }));
    await user.click(await screen.findByRole("option", { name: "Preferred subscription with reserves" }));
    await user.click(screen.getByRole("combobox", { name: "Add subscription" }));
    expect(screen.queryByRole("option", { name: "Other model" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Primary" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("option", { name: "Reserve" }));
    const list = screen.getByRole("list", { name: "Assigned subscriptions" });
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("Primary");
    await user.click(screen.getByRole("button", { name: "Move Reserve up" }));
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("Reserve");
    await user.click(screen.getByRole("button", { name: "Remove Primary" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByRole("listitem")).toHaveTextContent("Reserve");
  });
});
