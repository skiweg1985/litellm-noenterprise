import { getProxyBaseUrl } from "@/components/networking";
import { createApiClient, type RequestOptions } from "./client";
import type { components } from "./subscription-usage-schema";

export type ObservedUsage = components["schemas"]["ObservedUsage"];
export type SubscriptionUsage = components["schemas"]["SubscriptionUsage"];
export type UsageRange = { start_date: string; end_date: string; subscription_id?: string | null };
const client = createApiClient({ getBaseUrl: () => getProxyBaseUrl() ?? "" });

export function getObservedUsage(accessToken: string, range: UsageRange, signal?: AbortSignal) {
  const options: RequestOptions = {
    accessToken,
    credentials: "same-origin",
    query: range,
    signal,
  };
  return client.get<ObservedUsage>("/chatgpt/api/observed-usage", options);
}
