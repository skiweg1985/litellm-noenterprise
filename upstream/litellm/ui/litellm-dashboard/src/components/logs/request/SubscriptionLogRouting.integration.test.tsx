import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { UrlUpdateEvent } from "nuqs/adapters/testing";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import RequestLogsPanel from "./RequestLogsPanel";
import { uiSpendLogsCall } from "../../networking";

vi.mock("../../networking", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../networking")>()),
  uiSpendLogsCall: vi.fn(),
}));
vi.mock("@/components/key_team_helpers/filter_helpers", () => ({ fetchAllTeams: vi.fn().mockResolvedValue([]) }));
const onUrlUpdate = vi.fn<(event: UrlUpdateEvent) => void>();
const lastCall = () => vi.mocked(uiSpendLogsCall).mock.calls.at(-1)?.[0];
const respondWith = (data: unknown[]) => {
  const response = { data, total: data.length, page: 1, page_size: 50, total_pages: 1 };
  vi.mocked(uiSpendLogsCall).mockResolvedValue(response);
};
const renderPanel = (searchParams: string) =>
  renderWithProviders(
    <RequestLogsPanel accessToken="test-token" token="test-token" userRole="Admin" userID="user-1" isActive />,
    { searchParams, onUrlUpdate },
  );

describe("subscription log deep links", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    testQueryClient.clear();
    respondWith([]);
  });

  it("passes the subscription and exact linked time window to the server", async () => {
    renderPanel(
      "?subscription_id=89d0cecc-8f8b-4e62-a681-21b2ba1dab13&start_date=2026-11-01T10%3A15%3A30Z&end_date=2026-11-01T11%3A20%3A45Z",
    );
    await waitFor(() =>
      expect(lastCall()).toMatchObject({
        start_date: "2026-11-01 10:15:30",
        end_date: "2026-11-01 11:20:45",
        params: { subscription_id: "89d0cecc-8f8b-4e62-a681-21b2ba1dab13" },
      }),
    );
    expect(screen.getByText(/Subscription:/)).toBeInTheDocument();
  });

  it("clears subscription and time-window query state with Reset Filters", async () => {
    renderPanel(
      "?subscription_id=89d0cecc-8f8b-4e62-a681-21b2ba1dab13&start_date=2026-11-01T10%3A15%3A30Z&end_date=2026-11-01T11%3A20%3A45Z&unrelated=keep",
    );
    fireEvent.click(screen.getByRole("button", { name: "Reset Filters" }));
    await waitFor(() => expect(onUrlUpdate).toHaveBeenCalled());
    expect(onUrlUpdate.mock.calls.at(-1)?.[0].queryString).toBe("?unrelated=keep");
    await waitFor(() => expect(lastCall()?.params?.subscription_id).toBeUndefined());
  });
});
