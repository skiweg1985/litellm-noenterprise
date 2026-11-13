import { getProxyBaseUrl } from "@/components/networking";
import { createApiClient } from "./client";
import type { components } from "./oidc-links-schema";

export type LinkView = components["schemas"]["LinkView"];
export type UserChoice = components["schemas"]["UserChoice"];
export type LinksResponse = components["schemas"]["LinksResponse"];
export type LinkAction = "approve" | "reject" | "reopen" | "unlink";
type RevisionRequest = components["schemas"]["RevisionRequest"];
type ApproveRequest = components["schemas"]["ApproveRequest"];
const client = createApiClient({ getBaseUrl: () => getProxyBaseUrl() ?? "" });
const options = (accessToken: string) => ({ accessToken, credentials: "same-origin" as const });

export const getOIDCLinks = (accessToken: string, signal?: AbortSignal) =>
  client.get<LinksResponse>("/oidc/links", { ...options(accessToken), signal });

export const updateOIDCLink = (
  accessToken: string,
  identityKey: string,
  action: LinkAction,
  body: RevisionRequest | ApproveRequest,
) =>
  client.post<LinkView>(`/oidc/links/${encodeURIComponent(identityKey)}/${action}`, {
    ...options(accessToken),
    body,
  });
