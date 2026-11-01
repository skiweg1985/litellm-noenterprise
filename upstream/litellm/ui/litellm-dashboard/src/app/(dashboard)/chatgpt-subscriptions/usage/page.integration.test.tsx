import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, fireEvent, waitFor } from "@testing-library/react";
import { renderWithProviders, testQueryClient } from "../../../../../tests/test-utils";
import Page from "./page";

const { auth, getObservedUsage } = vi.hoisted(() => ({
  auth: { accessToken: "session", userId: "admin", userRole: "proxy_admin", isAuthorized: true, isLoading: false },
  getObservedUsage: vi.fn(),
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => auth }));
vi.mock("@/lib/http/subscriptionUsage", () => ({ getObservedUsage }));
const response = {
  start_date: "2026-11-01T10:00:00Z",
  end_date: "2026-11-01T11:00:00Z",
  coverage: {
    earliest_retained_log_at: "2026-10-31T00:00:00Z",
    unattributed_log_records: 3,
    cache_hit_log_records: 2,
    unknown_provider_log_records: 1,
    history_complete: false,
  },
  subscriptions: [
    {
      subscription_id: "5e29de88-cb41-42e8-a5fc-d7532817a5a4",
      display_name: "Team subscription",
      retired: true,
      historical_names: ["Old name"],
      logical_requests: 2,
      provider_attempts: 3,
      failed_attempts: 1,
      reserve_switches: 1,
      input_tokens: null,
      output_tokens: 8,
      unknown_input_attempts: 3,
      unknown_output_attempts: 1,
      revisions: [
        {
          connection_revision: "revision-one",
          logical_requests: 2,
          provider_attempts: 3,
          failed_attempts: 1,
          reserve_switches: 1,
          input_tokens: null,
          output_tokens: 8,
          unknown_input_attempts: 3,
          unknown_output_attempts: 1,
        },
      ],
    },
  ],
};
const params = "?start_date=2026-11-01T10:00:00Z&end_date=2026-11-01T11:00:00Z";

describe("Observed subscription usage", () => {
  beforeEach(() => {
    auth.userRole = "proxy_admin";
    getObservedUsage.mockReset().mockResolvedValue(response);
  });
  afterEach(() => {
    cleanup();
    testQueryClient.clear();
  });
  it("shows unknown observations and retired history separately from quota, with exact log links", async () => {
    renderWithProviders(<Page />, { searchParams: params });
    expect(await screen.findByRole("heading", { name: "Team subscription" })).toBeVisible();
    expect(screen.getByText("Unknown · 3 attempts without input usage")).toBeVisible();
    expect(screen.getByText("8 recorded · 1 attempt without output usage")).toBeVisible();
    expect(screen.getByText(/3 unattributed log records/)).toBeVisible();
    expect(screen.getByRole("link", { name: "View logs for Team subscription" })).toHaveAttribute(
      "href",
      expect.stringContaining("subscription_id=5e29de88-cb41-42e8-a5fc-d7532817a5a4"),
    );
    expect(screen.getByRole("link", { name: "View logs for Team subscription" })).toHaveAttribute(
      "href",
      expect.stringContaining("start_date=2026-11-01T10:00:00Z"),
    );
    fireEvent.click(screen.getByText("Connection history"));
    expect(screen.getByText("revision-one")).toBeVisible();
  });
  it("applies UTC bounds through URL state and preserves the subscription filter", async () => {
    const onUrlUpdate = vi.fn();
    renderWithProviders(<Page />, {
      searchParams: params + "&subscription_id=5e29de88-cb41-42e8-a5fc-d7532817a5a4&unrelated=keep",
      onUrlUpdate,
    });
    await screen.findByRole("heading", { name: "Team subscription" });
    fireEvent.change(screen.getByLabelText("From (UTC)"), { target: { value: "2026-10-31T10:00:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply period" }));
    await waitFor(() =>
      expect(getObservedUsage).toHaveBeenLastCalledWith(
        "session",
        {
          start_date: "2026-10-31T10:00:00.000Z",
          end_date: "2026-11-01T11:00:00.000Z",
          subscription_id: "5e29de88-cb41-42e8-a5fc-d7532817a5a4",
        },
        expect.anything(),
      ),
    );
    expect(onUrlUpdate.mock.lastCall?.[0].searchParams.get("unrelated")).toBe("keep");
  });
  it("denies non-administrators without requesting usage", () => {
    auth.userRole = "internal_user";
    renderWithProviders(<Page />);
    expect(screen.getByRole("alert")).toHaveTextContent("Proxy administrator access is required");
    expect(getObservedUsage).not.toHaveBeenCalled();
  });
  it("shows a retryable error instead of invented zero counts", async () => {
    getObservedUsage.mockRejectedValue(new Error("Unavailable"));
    renderWithProviders(<Page />, { searchParams: params });
    expect(await screen.findByRole("alert")).toHaveTextContent("Observed usage is unavailable");
    expect(screen.queryByRole("region", { name: "Usage for Team subscription" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh usage" })).toBeEnabled();
  });
});
