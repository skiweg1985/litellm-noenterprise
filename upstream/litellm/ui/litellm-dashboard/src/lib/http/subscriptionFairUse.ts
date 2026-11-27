import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";
import { createApiClient } from "./client";
import type { components } from "./subscription-fair-use-schema";

type Schema = components["schemas"];
export type Access = Schema["Access"];
export type FairUse = Schema["FairUse"];
export type Factors = Schema["Factors"];
export type ModelWeights = Schema["ModelWeights"];
export type ModelWeightsStatus = Schema["ModelWeightsStatus"];
export type AdminAccount = Schema["AdminAccount"];
export type PersonalAccount = Schema["PersonalAccount"];
export type Status = Schema["Status"];

const client = createApiClient({
  getBaseUrl: () => getProxyBaseUrl() ?? "",
  getAuthHeaderName: getGlobalLitellmHeaderName,
});
const accountPath = (name: string) => "/chatgpt/api/accounts/" + encodeURIComponent(name);
const base = "/chatgpt/api/fair-use";
const options = (accessToken: string, signal?: AbortSignal) => ({
  accessToken,
  credentials: "same-origin" as const,
  signal,
});

export function getAccess(token: string, name: string, signal?: AbortSignal) {
  return client.get<Access>(accountPath(name) + "/access", options(token, signal));
}

export function saveAccess(token: string, name: string, access: Access) {
  return client.put<Access>(accountPath(name) + "/access", { ...options(token), body: access });
}

export function getFairUse(token: string, name: string, signal?: AbortSignal) {
  return client.get<FairUse>(accountPath(name) + "/fair-use", options(token, signal));
}

export function saveFairUse(token: string, name: string, policy: FairUse) {
  return client.put<FairUse>(accountPath(name) + "/fair-use", { ...options(token), body: policy });
}

export function getFactors(token: string, signal?: AbortSignal) {
  return client.get<Factors>(base + "/factors", options(token, signal));
}

export function saveFactors(token: string, factors: Factors) {
  return client.put<Factors>(base + "/factors", { ...options(token), body: factors });
}

export function getModelWeights(token: string, signal?: AbortSignal) {
  return client.get<ModelWeightsStatus>(base + "/model-weights", options(token, signal));
}

export function saveModelWeights(token: string, overrides: ModelWeights) {
  return client.put<ModelWeightsStatus>(base + "/model-weights", { ...options(token), body: overrides });
}

export function getFairUseStatus(token: string, signal?: AbortSignal) {
  return client.get<Status>(base + "/status", options(token, signal));
}

export function getMySubscriptionUsage(token: string, signal?: AbortSignal) {
  return client.get<Schema["PersonalStatus"]>("/chatgpt/fair-use/me", options(token, signal));
}
