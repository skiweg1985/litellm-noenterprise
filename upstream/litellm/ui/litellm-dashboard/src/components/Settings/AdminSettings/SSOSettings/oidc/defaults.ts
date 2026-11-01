import type { APISettings, BrowserSettings } from "@/lib/http/oidcSettings";
import { emptyRule } from "./Fields";

export function browserDefaults(value: BrowserSettings | null): Required<BrowserSettings> {
  return {
    issuer: "",
    client_id: "",
    redirect_uri: typeof window === "undefined" ? "" : `${window.location.origin}/oidc/callback`,
    scopes: ["openid", "profile", "email"],
    subject_claim: "sub",
    access_rule: emptyRule(),
    role_mappings: [],
    default_role: "internal_user_viewer",
    session_ttl_seconds: 3600,
    retained_claims: ["roles", "groups", "scp", "scope"],
    mcp_access_rules: {},
    allow_insecure_http: false,
    ...value,
  };
}
export function apiDefaults(value: APISettings | null): Required<APISettings> {
  return {
    resource: typeof window === "undefined" ? "" : window.location.origin,
    audience: "",
    scope_claim: "scope",
    scope_format: "space_delimited",
    scopes: [{ name: "", token_value: "" }],
    token_types: ["at+jwt"],
    required_claims: [],
    ...value,
  };
}
export const roleChoices = [
  ["proxy_admin", "Administrator — manage the proxy"],
  ["proxy_admin_viewer", "Administrator viewer — read-only administration"],
  ["internal_user", "Internal user"],
  ["internal_user_viewer", "Internal viewer — limited access"],
  ["org_admin", "Organization administrator"],
  ["team", "Team"],
  ["customer", "Customer"],
] as const;
export const defaultRoleChoices = roleChoices.filter(
  ([role]) => role === "internal_user_viewer" || role === "customer",
);
