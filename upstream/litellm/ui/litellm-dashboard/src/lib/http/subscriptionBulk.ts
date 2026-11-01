import { getProxyBaseUrl } from "@/components/networking";
import { createApiClient } from "./client";
import type { components } from "./subscription-bulk-schema";

export type BulkPreview = components["schemas"]["BulkPreview"];
export type BulkChoice = components["schemas"]["ModelChoice"];
export type BulkResults = components["schemas"]["BulkResults"];
export type AssignmentPreview = components["schemas"]["AssignmentPreview"];
const client = createApiClient({ getBaseUrl: () => getProxyBaseUrl() ?? "" });
const endpoint = (account: string) => `/chatgpt/api/accounts/${encodeURIComponent(account)}/model-assignments`;

export function getBulkAssignments(accessToken: string, account: string, signal?: AbortSignal) {
  return client.get<BulkPreview>(endpoint(account), { accessToken, credentials: "same-origin", signal });
}

export function saveBulkAssignments(accessToken: string, account: string, models: BulkChoice[]) {
  return client.post<BulkResults>(endpoint(account), { accessToken, credentials: "same-origin", body: { models } });
}
