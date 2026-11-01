"use client";

import { useQuery } from "@tanstack/react-query";
import { createApiClient } from "@/lib/http/client";
import { getProxyBaseUrl } from "@/components/networking";

export const OIDC_BROWSER_MARKER = "oidc-browser-session";
const client = createApiClient({ getBaseUrl: () => getProxyBaseUrl() ?? "" });

/** The readable dashboard envelope is display data, not proof of a browser session. */
export function useOidcSession(accessToken: string | null, identity?: string | null) {
  const enabled = accessToken === OIDC_BROWSER_MARKER;
  const query = useQuery({
    queryKey: ["oidc-browser-session", identity],
    queryFn: () =>
      client.get<{ user_id: string; role: string; admin_access: boolean }>("/oidc/session", {
        credentials: "same-origin",
      }),
    enabled,
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
  return { ...query, enabled };
}
