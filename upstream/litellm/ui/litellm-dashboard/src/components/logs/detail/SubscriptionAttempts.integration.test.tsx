import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders as render } from "../../../../tests/test-utils";
import { SubscriptionAttempts } from "./SubscriptionAttempts";

const attempt = {
  attempt_id: "attempt-1",
  sequence: 1,
  subscription_id: "subscription-1",
  display_name: "Original name",
  connection_revision: "connection-1",
  model: "gpt-example",
  model_id: "deployment-1",
  started_at: "2026-11-01T12:00:00Z",
  ended_at: "2026-11-01T12:00:01Z",
  outcome: "failure",
  reason: "initial",
  input_tokens: null,
  output_tokens: null,
};

describe("subscription attempts in request details", () => {
  it("shows the recorded name, identity and outcome of each attempt in order", () => {
    render(
      <SubscriptionAttempts
        metadata={{
          subscription_attempts: [
            attempt,
            {
              ...attempt,
              attempt_id: "attempt-2",
              sequence: 2,
              subscription_id: "subscription-2",
              display_name: "Reserve",
              connection_revision: "connection-2",
              outcome: "success",
              reason: "fallback",
              input_tokens: 12,
              output_tokens: 4,
            },
          ],
        }}
        provider="chatgpt"
        model="chatgpt/gpt-example"
      />,
    );
    const list = screen.getByRole("list", { name: "Subscription attempts" });
    const entries = within(list).getAllByRole("listitem");
    expect(within(entries[0]).getByText("Original name")).toBeInTheDocument();
    expect(within(entries[0]).getByText("subscription-1")).toBeInTheDocument();
    expect(within(entries[0]).getByText("connection-1")).toBeInTheDocument();
    expect(within(entries[0]).getByText("Failure · Initial attempt")).toBeInTheDocument();
    expect(within(entries[0]).getByText("Unknown / Unknown")).toBeInTheDocument();
    expect(within(entries[1]).getByText("Success · Fallback")).toBeInTheDocument();
    expect(within(entries[1]).getByText("12 / 4")).toBeInTheDocument();
  });

  it("filters by the recorded subscription without copying its ID and preserves the time window", async () => {
    const onUrlUpdate = vi.fn();
    render(
      <SubscriptionAttempts
        metadata={{ subscription_attempts: [attempt] }}
        provider="chatgpt"
        model="chatgpt/gpt-example"
      />,
      {
        searchParams:
          "?log_id=request-1&session_id=session-1&start_date=2026-11-01T10%3A15%3A30Z&end_date=2026-11-01T11%3A20%3A45Z",
        onUrlUpdate,
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Filter logs for Original name" }));
    await waitFor(() => expect(onUrlUpdate).toHaveBeenCalled());
    const query = onUrlUpdate.mock.calls.at(-1)?.[0].queryString;
    expect(query).toContain("subscription_id=subscription-1");
    expect(query).toContain("start_date=");
    expect(query).toContain("end_date=");
    expect(query).not.toContain("log_id=");
    expect(query).not.toContain("session_id=");
  });

  it("does not infer a legacy subscription from the model name", () => {
    render(<SubscriptionAttempts metadata={{}} provider="chatgpt" model="chatgpt/old-account/gpt-example" />);
    expect(screen.getByText(/Unknown\/legacy/)).toBeInTheDocument();
    expect(screen.queryByText("old-account")).not.toBeInTheDocument();
  });

  it("distinguishes a known request without a provider attempt from legacy logs", () => {
    render(
      <SubscriptionAttempts
        metadata={{ subscription_attempts: [], subscription_logical_id: "logical-1" }}
        provider="chatgpt"
        model="chatgpt/gpt-example"
      />,
    );
    expect(screen.getByText("No subscription provider attempt was recorded for this request.")).toBeInTheDocument();
  });
});
