import { screen, renderWithProviders } from "../../../tests/test-utils";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { getSubscriptions } from "@/lib/http/chatgptSubscriptions";
import { SubscriptionPicker } from "./SubscriptionPicker";

vi.mock("@/lib/http/chatgptSubscriptions", () => ({ getSubscriptions: vi.fn() }));
const fixture = {
  account: "seat-a",
  subscription_id: "sub-one",
  display_name: "Development",
  state: "ok",
  token_valid: true,
  retired: false,
  credentials: [],
  models: [],
  available_models: { models: [{ slug: "example-model", name: "Example", configured: false }], missing: [] },
};

describe("SubscriptionPicker", () => {
  it("lets an administrator choose a named compatible subscription", async () => {
    vi.mocked(getSubscriptions).mockResolvedValue({ accounts: [fixture], problems: [] } as never);
    const selected = vi.fn();
    renderWithProviders(
      <SubscriptionPicker accessToken="fixture" value={null} onChange={selected} model="chatgpt/example-model" />,
    );
    const picker = await screen.findByRole("combobox", { name: "ChatGPT subscription" });
    await userEvent.click(picker);
    await userEvent.click(await screen.findByRole("option", { name: "Development" }));
    expect(selected).toHaveBeenCalledWith("sub-one");
  });

  it("keeps the assignment visible when its catalog is unavailable", async () => {
    vi.mocked(getSubscriptions).mockResolvedValue({
      accounts: [{ ...fixture, available_models: null }],
      problems: [],
    } as never);
    renderWithProviders(
      <SubscriptionPicker
        accessToken="fixture-unavailable"
        value="sub-one"
        onChange={vi.fn()}
        model="chatgpt/example-model"
      />,
    );
    expect(await screen.findByText(/catalog is unavailable/i)).toBeVisible();
    expect(screen.getByRole("combobox", { name: "ChatGPT subscription" })).toHaveValue("Development");
  });
  it("hides cached subscription names after a failed refresh", async () => {
    vi.mocked(getSubscriptions)
      .mockResolvedValueOnce({ accounts: [fixture], problems: [] } as never)
      .mockRejectedValueOnce(new Error("Unauthorized"));
    renderWithProviders(<SubscriptionPicker accessToken="fixture-expired" value="sub-one" onChange={vi.fn()} />);
    expect(await screen.findByDisplayValue("Development")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Refresh subscriptions" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Subscriptions could not be loaded");
    expect(screen.queryByDisplayValue("Development")).not.toBeInTheDocument();
  });
});
