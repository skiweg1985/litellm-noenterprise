# Existing capabilities in LiteLLM 1.106.6

This inventory is scoped to the reviewed baseline revision recorded in the
[import policy](../upstream/policy.json). Source availability does not prove that
a complete flow works without enterprise dependencies.

| Area | Existing eligible source | Integration implication |
|---|---|---|
| Personal connections | Dashboard `/connect` and `/chat/integrations`, `ConnectFlowSurface`, `MCPAppsPanel` | Reuse these views; a new standalone page is not the default plan. |
| Browser MCP authorization | `useUserMcpOAuthFlow` | Reuse the OAuth interaction, while checking its UI-token and administration-page assumptions. |
| Managed credentials | MCP management endpoints under `/v1/mcp`, per-user storage in the MCP database adapter | Existing connect/status/disconnect and encrypted storage are outside enterprise. Test identity isolation and permission checks before extending. |
| Client-supplied credentials | OAuth Delegate support in the MCP gateway | Keep LiteLLM caller authentication distinct from upstream MCP credentials. |
| OIDC sign-in | Existing generic/provider SSO code | Some paths impose premium or user-count gates. The project needs an independent admission and role integration. |
| API authentication | Existing custom-auth extension point | It replaces authentication rather than falling back automatically to API keys; preserve unrelated API-key behavior explicitly. |
| JWT configuration | Existing JWT authentication facilities | Enterprise gates exist; do not globally enable premium mode. |

The image smoke check verifies rebuilt `/ui/connect/` assets, registered OAuth
routes and anonymous credential denial. The independent browser suite exercises
the same page with the configured identity/database and controlled MCP provider.
The retained components are `ConnectFlowSurface`, `MCPAppsPanel` and
`useUserMcpOAuthFlow`; changes add session gating, truthful connection states,
server-side managed token exchange, accessible controls and phone layouts.
See [personal connection usage and checks](identity.md#manage-personal-connections).

## Test reuse

The imported suite includes integration HTTP helpers, a controlled MCP server,
and browser OIDC fixtures. Prefer the public HTTP/MCP and browser boundaries.
Some upstream MCP/OIDC tests require a live identity provider, license flag, or
inspect database records directly; do not treat them unchanged as acceptance of
this distribution. Adapt their external-service patterns to isolated tests.

## Baseline limitations

The optional native Rust bridge is not built; upstream's Python fallback is used.
The separately licensed capability classifier is excluded and its configuration
is rejected. The baseline does not qualify every upstream optional integration.
No production provider or user database was changed for baseline validation.
