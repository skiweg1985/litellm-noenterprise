import { getProxyBaseUrl } from "@/components/networking";
import { createApiClient } from "./client";
import type { components } from "./oidc-settings-schema";

export type BrowserSettings = components["schemas"]["BrowserSettings"];
export type APISettings = components["schemas"]["APISettings"];
export type ClaimRule = components["schemas"]["ClaimRule"];
export type RoleMapping = components["schemas"]["RoleMapping"];
export type SettingsResponse = components["schemas"]["SettingsResponse"];
export type SaveSettingsRequest = components["schemas"]["SaveSettingsRequest"];
export type SaveSettingsResponse = components["schemas"]["SaveSettingsResponse"];
export type CheckProviderResponse = components["schemas"]["CheckProviderResponse"];

const client = createApiClient({ getBaseUrl: () => getProxyBaseUrl() ?? "" });
const options = (accessToken: string) => ({ accessToken, credentials: "same-origin" as const });

export const getOIDCSettings = (accessToken: string, signal?: AbortSignal) =>
  client.get<SettingsResponse>("/oidc/settings", { ...options(accessToken), signal });
export const saveOIDCSettings = (accessToken: string, body: SaveSettingsRequest) =>
  client.put<SaveSettingsResponse>("/oidc/settings", { ...options(accessToken), body });
export const disableOIDCSettings = (accessToken: string, expectedRevision: number) =>
  client.post<SaveSettingsResponse>("/oidc/settings/disable", {
    ...options(accessToken),
    body: { expected_revision: expectedRevision },
  });
export const checkOIDCProvider = (accessToken: string, issuer: string, allowInsecureHttp: boolean) =>
  client.post<CheckProviderResponse>("/oidc/settings/check", {
    ...options(accessToken),
    body: { issuer, allow_insecure_http: allowInsecureHttp },
  });
