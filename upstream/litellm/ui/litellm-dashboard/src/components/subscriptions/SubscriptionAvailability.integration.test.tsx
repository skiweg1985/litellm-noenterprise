import { beforeEach, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { renderWithProviders, screen, testQueryClient } from "../../../tests/test-utils";
import { getSubscriptionAvailability } from "@/lib/http/chatgptSubscriptions";
import { SubscriptionAvailability } from "./SubscriptionAvailability";

vi.mock("@/lib/http/chatgptSubscriptions", () => ({ getSubscriptionAvailability: vi.fn() }));
const unavailable = {
  model_id: "model",
  assignment_revision: "revision",
  state: "unavailable",
  checked_at: 1,
  cooldown_scope: "worker",
  members: [{ subscription_id: "seat", state: "cooldown", retry_at: 100 }],
} as const;
beforeEach(() => testQueryClient.clear());
it("shows model cooldown separately from connection state and refreshes recovery", async () => {
  vi.mocked(getSubscriptionAvailability)
    .mockResolvedValueOnce(unavailable as never)
    .mockResolvedValueOnce({
      ...unavailable,
      state: "eligible",
      members: [{ subscription_id: "seat", state: "eligible" }],
    } as never);
  renderWithProviders(<SubscriptionAvailability accessToken="fixture" modelId="model" names={{ seat: "Reserve" }} />);
  expect(await screen.findByRole("status")).toHaveTextContent("No assigned subscription is currently eligible");
  expect(screen.getByText(/Reserve: Cooling down/)).toBeVisible();
  expect(screen.getByText(/This worker only/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Refresh routing availability" }));
  expect(await screen.findByText(/At least one assigned subscription is eligible/)).toBeVisible();
  expect(screen.queryByText(/Reserve: Cooling down/)).not.toBeInTheDocument();
});
it("does not present stale eligibility when refresh fails", async () => {
  vi.mocked(getSubscriptionAvailability).mockRejectedValueOnce(new Error("Unavailable"));
  renderWithProviders(<SubscriptionAvailability accessToken="fixture" modelId="model" names={{ seat: "Reserve" }} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Routing availability could not be checked");
  expect(screen.queryByText(/eligible/)).not.toBeInTheDocument();
});
