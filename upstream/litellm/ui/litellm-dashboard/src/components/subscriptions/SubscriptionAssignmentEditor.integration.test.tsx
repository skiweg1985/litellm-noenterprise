import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, testQueryClient, within } from "../../../tests/test-utils";
import { getSubscriptions } from "@/lib/http/chatgptSubscriptions";
import { SubscriptionAssignmentEditor, type SubscriptionSelection } from "./SubscriptionAssignmentEditor";

vi.mock("@/lib/http/chatgptSubscriptions", () => ({ getSubscriptions: vi.fn() }));
const account = (id: string, name: string, slug = "example") => ({
  account: id,
  subscription_id: id,
  display_name: name,
  token_valid: true,
  retired: false,
  available_models: { models: [{ slug, name: slug }], missing: [] },
});
function Editor() {
  const [value, setValue] = useState<SubscriptionSelection>({ mode: "fixed", subscription_ids: ["first"] });
  return (
    <SubscriptionAssignmentEditor
      accessToken="fixture"
      value={value}
      onChange={setValue}
      models={["chatgpt/example"]}
    />
  );
}

describe("subscription assignment editor", () => {
  beforeEach(() => {
    testQueryClient.clear();
    vi.mocked(getSubscriptions).mockResolvedValue({
      accounts: [
        account("first", "Primary"),
        account("second", "Reserve"),
        account("wrong", "Other model", "different"),
      ],
      problems: [],
    } as never);
  });
  it("adds only compatible reserves and lets the administrator order and remove them", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Editor />);
    await user.click(await screen.findByRole("combobox", { name: "Subscription routing mode" }));
    await user.click(await screen.findByRole("option", { name: "Preferred subscription with reserves" }));
    await user.click(screen.getByRole("combobox", { name: "Add subscription" }));
    expect(screen.queryByRole("option", { name: "Other model" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Primary" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("option", { name: "Reserve" }));
    const list = screen.getByRole("list", { name: "ChatGPT subscriptions" });
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("Primary");
    await user.click(screen.getByRole("button", { name: "Move Reserve up" }));
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("Reserve");
    await user.click(screen.getByRole("button", { name: "Remove Primary" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByRole("listitem")).toHaveTextContent("Reserve");
  });
  it("keeps unavailable assignments visible and directs the administrator to sign in or remove them", async () => {
    vi.mocked(getSubscriptions).mockResolvedValue({
      accounts: [{ ...account("first", "Primary"), token_valid: false, state: "credential_rejected" }],
      problems: [],
    } as never);
    renderWithProviders(
      <SubscriptionAssignmentEditor
        accessToken="unavailable-fixture"
        value={{ mode: "priority", subscription_ids: ["first"] }}
        onChange={vi.fn()}
        models={["chatgpt/example"]}
      />,
    );
    expect(await screen.findByRole("status")).toHaveTextContent("No assigned subscription is available");
    expect(screen.getByText("Sign-in was rejected. Reconnect this subscription.")).toBeVisible();
    expect(screen.getByRole("link", { name: "subscription management" })).toHaveAttribute(
      "href",
      "/ui/chatgpt-subscriptions/",
    );
    expect(screen.getByRole("button", { name: "Remove Primary" })).toBeEnabled();
    expect(screen.getByRole("list", { name: "ChatGPT subscriptions" })).toHaveTextContent("Primary");
  });

  it("hides cached member names if the administrator loses access on refresh", async () => {
    vi.mocked(getSubscriptions)
      .mockResolvedValueOnce({ accounts: [account("first", "Primary")], problems: [] } as never)
      .mockRejectedValueOnce(new Error("Forbidden"));
    renderWithProviders(
      <SubscriptionAssignmentEditor
        accessToken="expired-fixture"
        value={{ mode: "priority", subscription_ids: ["first"] }}
        onChange={vi.fn()}
      />,
    );
    await screen.findByRole("button", { name: "Remove Primary" });
    await userEvent.click(screen.getByRole("button", { name: "Refresh subscriptions" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Subscriptions could not be loaded");
    expect(screen.queryByText(/Primary/)).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Add subscription" })).toBeDisabled();
  });
});
