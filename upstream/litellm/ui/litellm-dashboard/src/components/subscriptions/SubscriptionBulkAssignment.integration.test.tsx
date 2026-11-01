import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithProviders, testQueryClient } from "../../../tests/test-utils";
import { getBulkAssignments, saveBulkAssignments } from "@/lib/http/subscriptionBulk";
import { SubscriptionBulkAssignment } from "./SubscriptionBulkAssignment";
import { ApiError } from "@/lib/http/client";

vi.mock("@/lib/http/subscriptionBulk", () => ({ getBulkAssignments: vi.fn(), saveBulkAssignments: vi.fn() }));
const assignment = (mode: "fixed" | "priority" | "distribution", names = ["Original"]) => ({
  mode,
  subscription_ids: names.map((name) => name.toLowerCase()),
  names,
});
const plan = {
  subscription_id: "new",
  display_name: "New",
  models: [
    {
      model_id: "reserve",
      model_name: "Assistant",
      upstream_model: "gpt-example",
      version: "version-a",
      current: assignment("priority"),
      proposed: assignment("priority", ["Original", "New"]),
      eligible: true,
      selected: true,
      reason: null,
    },
    {
      model_id: "fixed",
      model_name: "Stable",
      upstream_model: "gpt-example",
      version: "version-b",
      current: assignment("fixed"),
      proposed: null,
      eligible: true,
      selected: false,
      reason: null,
    },
    {
      model_id: "config",
      model_name: "Configured",
      upstream_model: "gpt-example",
      version: null,
      current: assignment("fixed"),
      proposed: null,
      eligible: false,
      selected: false,
      reason: "Configuration-backed model. Edit its server configuration instead.",
    },
  ],
};
describe("bulk subscription assignment review", () => {
  beforeEach(() => {
    testQueryClient.clear();
    vi.mocked(getBulkAssignments).mockReset().mockResolvedValue(plan);
    vi.mocked(saveBulkAssignments)
      .mockReset()
      .mockResolvedValue({ results: [{ model_id: "fixed", status: "saved", message: "Assignment saved." }] });
  });

  it("requires an explicit fixed-model choice and saves only selected reviewed models", async () => {
    renderWithProviders(<SubscriptionBulkAssignment accessToken="fixture" account="new-account" onClose={vi.fn()} />);
    const reserve = await screen.findByRole("region", { name: "Model Assistant" });
    expect(within(reserve).getByRole("checkbox", { name: "Select Assistant" })).toBeChecked();
    expect(within(reserve).getByText("Original → New")).toBeVisible();
    const fixed = screen.getByRole("region", { name: "Model Stable" });
    expect(within(fixed).getByRole("checkbox", { name: "Select Stable" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select Configured" })).toBeDisabled();
    fireEvent.click(within(reserve).getByRole("checkbox", { name: "Select Assistant" }));
    fireEvent.click(within(fixed).getByRole("checkbox", { name: "Select Stable" }));
    expect(screen.getByRole("button", { name: "Save selected models" })).toBeDisabled();
    fireEvent.change(within(fixed).getByRole("combobox", { name: "Change for Stable" }), {
      target: { value: "replace" },
    });
    expect(within(fixed).getByText("New")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Save selected models" }));
    await waitFor(() =>
      expect(saveBulkAssignments).toHaveBeenCalledWith("fixture", "new-account", [
        { model_id: "fixed", version: "version-b", action: "replace" },
      ]),
    );
    expect(await screen.findByText("Assignment saved.")).toBeVisible();
  });

  it("keeps successful row results while retrying only a failed selection", async () => {
    vi.mocked(saveBulkAssignments)
      .mockResolvedValueOnce({
        results: [
          { model_id: "reserve", status: "saved", message: "Assistant saved." },
          { model_id: "fixed", status: "error", message: "Catalog temporarily unavailable." },
        ],
      })
      .mockResolvedValueOnce({ results: [{ model_id: "fixed", status: "saved", message: "Stable saved." }] });
    renderWithProviders(<SubscriptionBulkAssignment accessToken="fixture" account="new-account" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Stable" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Change for Stable" }), {
      target: { value: "distribution" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save selected models" }));
    expect(await screen.findByText("Catalog temporarily unavailable.")).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "Select Assistant" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save selected models" }));
    expect(await screen.findByText("Stable saved.")).toBeVisible();
    expect(screen.getByText("Assistant saved.")).toBeVisible();
    expect(vi.mocked(saveBulkAssignments).mock.calls[1][2]).toEqual([
      { model_id: "fixed", version: "version-b", action: "distribution" },
    ]);
  });

  it("requires a refreshed review after a version conflict", async () => {
    vi.mocked(saveBulkAssignments).mockResolvedValue({
      results: [{ model_id: "reserve", status: "conflict", message: "Model changed. Refresh the review." }],
    });
    renderWithProviders(<SubscriptionBulkAssignment accessToken="fixture" account="new-account" onClose={vi.fn()} />);
    await screen.findByRole("checkbox", { name: "Select Assistant" });
    fireEvent.click(screen.getByRole("button", { name: "Save selected models" }));
    expect(await screen.findByText("Model changed. Refresh the review.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save selected models" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh review" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save selected models" })).toBeEnabled());
  });

  it("hides cached model details when authorization expires", async () => {
    renderWithProviders(<SubscriptionBulkAssignment accessToken="fixture" account="new-account" onClose={vi.fn()} />);
    await screen.findByRole("region", { name: "Model Assistant" });
    vi.mocked(saveBulkAssignments).mockRejectedValue(new ApiError("Forbidden", 403, {}));
    fireEvent.click(screen.getByRole("button", { name: "Save selected models" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("region", { name: "Model Assistant" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save selected models" })).toBeDisabled();
  });

  it("does not claim compatibility when the catalog fails and offers a fresh review", async () => {
    vi.mocked(getBulkAssignments)
      .mockRejectedValueOnce(new ApiError("Catalog unavailable", 503, {}))
      .mockResolvedValueOnce(plan);
    renderWithProviders(<SubscriptionBulkAssignment accessToken="fixture" account="new-account" onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("catalog is unavailable");
    expect(screen.queryByRole("region", { name: "Model Assistant" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save selected models" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh review" }));
    expect(await screen.findByRole("checkbox", { name: "Select Assistant" })).toBeChecked();
  });
});
