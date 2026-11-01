import type { SubscriptionSelection } from "./SubscriptionAssignmentEditor";
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
  return withSubscriptionSelection(params, info, undefined, subscriptionId);
}

export function withSubscriptionSelection<T extends Record<string, unknown>, I extends Record<string, unknown>>(
  params: T,
  info: I,
  selection: SubscriptionSelection | null | undefined,
  legacyId?: string | null,
) {
  const ids = selection?.subscription_ids ?? (legacyId ? [legacyId] : []);
  if (!ids.length || !isChatGPTModel(params)) return { params, info };
  const {
    api_key: _apiKey,
    litellm_credential_name: _credential,
    subscription_id: _field,
    subscription_selection: _selection,
    ...safeParams
  } = params;
  const previous = info.subscription_assignment as SubscriptionAssignment | undefined;
  const assignment: SubscriptionAssignmentInput = {
    mode: selection?.mode ?? "fixed",
    subscription_ids: ids,
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
