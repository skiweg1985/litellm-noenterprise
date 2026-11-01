import type { LogEntry } from "./types";

export interface SubscriptionAttempt {
  attempt_id: string;
  sequence: number;
  subscription_id: string;
  display_name: string;
  connection_revision: string | null;
  model: string | null;
  model_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  outcome: string;
  reason: string;
  input_tokens: number | null;
  output_tokens: number | null;
}

const text = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);
const tokens = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;

/** Read persisted snapshots only. Current account/deployment configuration is never consulted. */
export function subscriptionAttempts(metadata: Record<string, unknown> | undefined): SubscriptionAttempt[] {
  const records = metadata?.subscription_attempts;
  if (!Array.isArray(records)) return [];
  const seen = new Set<string>();
  return records
    .flatMap((value: unknown) => {
      if (typeof value !== "object" || value === null) return [];
      const row = value as Record<string, unknown>;
      const attemptId = text(row.attempt_id);
      const subscriptionId = text(row.subscription_id);
      if (!attemptId || !subscriptionId || seen.has(attemptId)) return [];
      seen.add(attemptId);
      return [
        {
          attempt_id: attemptId,
          subscription_id: subscriptionId,
          display_name: text(row.display_name) ?? subscriptionId,
          connection_revision: text(row.connection_revision),
          sequence: typeof row.sequence === "number" && Number.isFinite(row.sequence) ? row.sequence : seen.size,
          model: text(row.model),
          model_id: text(row.model_id),
          started_at: text(row.started_at),
          ended_at: text(row.ended_at),
          outcome: text(row.outcome) ?? "unknown",
          reason: text(row.reason) ?? "unknown",
          input_tokens: tokens(row.input_tokens),
          output_tokens: tokens(row.output_tokens),
        },
      ];
    })
    .sort((a, b) => a.sequence - b.sequence);
}

export function subscriptionAttributionLabel(
  log: Pick<LogEntry, "metadata" | "custom_llm_provider" | "model">,
): string {
  const attempts = subscriptionAttempts(log.metadata);
  if (attempts.length) return [...new Set(attempts.map((attempt) => attempt.display_name))].join(", ");
  if (
    Array.isArray(log.metadata?.subscription_attempts) &&
    log.metadata.subscription_attempts.length === 0 &&
    typeof log.metadata.subscription_logical_id === "string"
  )
    return "No provider attempt";
  return log.custom_llm_provider === "chatgpt" || log.model.startsWith("chatgpt/") ? "Unknown/legacy" : "—";
}
