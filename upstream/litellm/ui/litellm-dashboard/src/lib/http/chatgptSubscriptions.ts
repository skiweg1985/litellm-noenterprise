import { getProxyBaseUrl } from "@/components/networking";
import { createApiClient } from "./client";
import type { components } from "./subscriptions-schema";

export interface SubscriptionIdentity {
  email?: string | null;
  account_id?: string | null;
  plan?: string | null;
}

export interface QuotaWindow {
  remaining_percent: number | null;
  window_seconds: number | null;
  reset_at: number | null;
}

type SubscriptionMetadata = Pick<
  components["schemas"]["SubscriptionDescription"],
  "account" | "subscription_id" | "display_name" | "connection_revision" | "retired" | "state" | "token_valid"
>;

export interface SubscriptionAccount extends SubscriptionIdentity, SubscriptionMetadata {
  token_expires_at: number | null;
  last_refresh_at: number | null;
  auto_login: boolean;
  login?: { user_code: string; verification_url: string; expires_at: number } | null;
  known_account?: SubscriptionIdentity | null;
  pending_account?: SubscriptionIdentity | null;
  error?: { code: string; at: number } | null;
  quota?: {
    limits: { id: string; allowed: boolean | null; primary: QuotaWindow | null; secondary: QuotaWindow | null }[];
  } | null;
  quota_error?: string | null;
  available_models?: {
    models: { slug: string; name: string; configured: boolean; context_window: number | null; reasoning: string[] }[];
    missing: { model_name: string; slug: string }[];
  } | null;
  available_models_error?: string | null;
  credentials: string[];
  models: string[];
  requests_24h: number | null;
}

export interface SubscriptionOverview {
  accounts: SubscriptionAccount[];
  problems: { deployment: string; credential?: string | null; account?: string | null; problem: string }[];
}

export type SubscriptionAction =
  | "login"
  | "login/cancel"
  | "pending/confirm"
  | "pending/discard"
  | "refresh"
  | "logout"
  | "retire";

const client = createApiClient({ getBaseUrl: () => getProxyBaseUrl() ?? "" });
const endpoint = "/chatgpt/api/accounts";

export function getSubscriptions(accessToken: string, signal?: AbortSignal) {
  return client.get<SubscriptionOverview>(endpoint, { accessToken, credentials: "same-origin", signal });
}

export function updateSubscription(accessToken: string, name: string, action: SubscriptionAction) {
  return client.post<SubscriptionAccount>(`${endpoint}/${encodeURIComponent(name)}/${action}`, {
    accessToken,
    credentials: "same-origin",
  });
}

export function createSubscription(accessToken: string, displayName: string) {
  return client.post<SubscriptionAccount>(endpoint, {
    accessToken,
    credentials: "same-origin",
    body: { display_name: displayName } satisfies components["schemas"]["SubscriptionName"],
  });
}

export function renameSubscription(accessToken: string, name: string, displayName: string) {
  return client.patch<SubscriptionAccount>(`${endpoint}/${encodeURIComponent(name)}`, {
    accessToken,
    credentials: "same-origin",
    body: { display_name: displayName } satisfies components["schemas"]["SubscriptionName"],
  });
}
