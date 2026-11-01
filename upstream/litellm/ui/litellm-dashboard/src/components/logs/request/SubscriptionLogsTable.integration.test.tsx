import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DataTable } from "@/components/shared/DataTable";
import type { LogEntry } from "../types";
import { getRequestLogsTableColumns } from "./RequestLogsTableColumns";

const logEntry = (overrides: Partial<LogEntry>): LogEntry => ({
  request_id: "req-1",
  api_key: "key-1",
  team_id: "team-1",
  model: "gpt-4o",
  model_id: "model-1",
  call_type: "acompletion",
  spend: 0,
  total_tokens: 10,
  prompt_tokens: 5,
  completion_tokens: 5,
  startTime: "2026-07-07T09:50:13Z",
  endTime: "2026-07-07T09:50:14Z",
  cache_hit: "false",
  messages: [],
  response: {},
  ...overrides,
});

const noopDeps = { onKeyHashClick: vi.fn(), onSessionClick: vi.fn() };

function renderRows(rows: LogEntry[], deps = noopDeps) {
  render(
    <DataTable
      data={rows}
      columns={getRequestLogsTableColumns(deps)}
      getRowId={(row) => row.request_id}
      size="compact"
    />,
  );
}

describe("subscription attribution in the log table", () => {
  it("shows recorded subscription names alongside the unchanged model and unknown legacy rows", () => {
    renderRows([
      logEntry({
        model: "public-model-name",
        metadata: {
          subscription_attempts: [
            { attempt_id: "one", subscription_id: "subscription-one", display_name: "First seat", sequence: 1 },
            { attempt_id: "two", subscription_id: "subscription-two", display_name: "Reserve seat", sequence: 2 },
          ],
        },
      }),
      logEntry({ request_id: "legacy", model: "chatgpt/legacy-model", custom_llm_provider: "chatgpt" }),
    ]);
    expect(screen.getByRole("columnheader", { name: "Subscription" })).toBeInTheDocument();
    expect(screen.getByText("First seat, Reserve seat")).toBeInTheDocument();
    expect(screen.getByText("public-model-name")).toBeInTheDocument();
    expect(screen.getByText("Unknown/legacy")).toBeInTheDocument();
  });
});
