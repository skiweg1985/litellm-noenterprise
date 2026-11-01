import type { components } from "@/lib/http/subscriptions-schema";

type SubscriptionAssignment = components["schemas"]["SubscriptionAssignment"];
// The response always includes revision; new/legacy input does not have one yet.
type SubscriptionAssignmentInput = Omit<SubscriptionAssignment, "revision"> &
  Partial<Pick<SubscriptionAssignment, "revision">>;

/** The dashboard submits a stable subscription identity; the server resolves credentials. */
export function withFixedSubscription<T extends Record<string, unknown>, I extends Record<string, unknown>>(
  params: T,
  info: I,
  subscriptionId: string | null | undefined,
) {
  if (!subscriptionId || !isChatGPTModel(params)) return { params, info };
  const { api_key: _apiKey, litellm_credential_name: _credential, subscription_id: _field, ...safeParams } = params;
  const previous = info.subscription_assignment as SubscriptionAssignment | undefined;
  const assignment: SubscriptionAssignmentInput = {
    mode: "fixed",
    subscription_ids: [subscriptionId],
    ...(typeof previous?.revision === "string" ? { revision: previous.revision } : {}),
  };
  return { params: safeParams, info: { ...info, subscription_assignment: assignment } };
}

export function isChatGPTModel(params: { model?: unknown; custom_llm_provider?: unknown }) {
  return (
    String(params.model ?? "").startsWith("chatgpt/") ||
    ["chatgpt", "chatgpt subscription"].includes(String(params.custom_llm_provider ?? "").toLowerCase())
  );
}
