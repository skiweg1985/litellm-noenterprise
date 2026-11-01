# Configure user identity

The independent OIDC integration uses the existing LiteLLM user database and
personal connection page. It supports independent browser and provider-token API
grants and personal MCP connections without enabling premium features.

## Configure a provider

Register a confidential web application with an exact callback URL, for example
`https://llm.example.org/oidc/callback`. Enable the authorization-code flow and
put the admission and role claims in its ID token. Configure LiteLLM:

```yaml
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  oidc_identity:
    issuer: https://id.example.org/tenant
    client_id: litellm
    client_secret_env: LITELLM_OIDC_CLIENT_SECRET
    redirect_uri: https://llm.example.org/oidc/callback
    subject_claim: sub
    scopes: [openid, profile, email]
    access_rule:
      claim: roles
      any_of: [litellm-access]
    role_mappings:
      - rule: {claim: roles, any_of: [litellm-admin]}
        role: proxy_admin
      - rule: {claim: roles, any_of: [litellm-admin-reader]}
        role: proxy_admin_viewer
    default_role: internal_user_viewer
```

Supply `DATABASE_URL`, a strong master key, the provider client secret, and
`LITELLM_OIDC_SESSION_SECRET` through your deployment's secret mechanism. The
session secret must contain at least 32 random characters and be identical on
all proxy replicas. OIDC startup fails without these prerequisites or when a
replacing `custom_auth` hook is also configured. Use HTTPS externally and for
the issuer. `allow_insecure_http` is only for isolated development fixtures.

Discovery must report the exact configured issuer. The current integration
supports RS256 and ES256 ID-token signatures and discovery-advertised
`client_secret_basic` or `client_secret_post` authentication. It uses PKCE S256,
state, and nonce. Claims are taken from the verified ID token; there is no
userinfo lookup. Providers requiring other client authentication methods need
an additional implementation.

For Entra, use a tenant-specific issuer and consider `subject_claim: oid` when
browser and API client registrations must identify the same directory object.
Configure that immutable claim in both token types. Never use email as the
stable subject. Real Entra compatibility still requires validation against the
chosen tenant and app registrations.

The validated [OIDCSettings schema](../upstream/litellm/litellm/proxy/auth/oidc_identity.py)
is the complete configuration reference. A claim rule accepts a string or
array of strings. For a space-separated scope claim, set
`value_format: space_delimited`. Nested claim paths such as `realm_access.roles`
are supported. `retained_claims` controls additional validated claim values
saved for later policy evaluation; identity, admission, and role claims are
always retained.

## Sign in and manage sessions

Open `/oidc/login`, or use the existing `/sso/key/generate` login entry point.
An eligible first login creates a user identified by issuer and the configured
stable subject. Repeated login updates that same account, applies the same
admission rule, and selects the first matching role mapping. An eligible user
with no matching mapping receives the configured minimal default role.
Email is informational and never links accounts.

Administrators land on `/ui/`; other users land on `/ui/connect/`. With
`DISABLE_ADMIN_UI=true`, OIDC administrators also use the personal page;
the administration interface remains disabled. Personal
sessions can use the existing connection endpoints but cannot administer users,
keys, models, or MCP servers. Existing unrelated native API keys retain their
normal behavior. The provider access token returned during login is discarded;
this browser flow does not turn that token into an API credential. Configure
the separate API grant below for direct client access.

The readable dashboard cookie contains only a nonsecret marker and display
information. Authentication also requires a separate encrypted HttpOnly cookie
and an unexpired database session record. Each browser has its own revocable
record and its own downstream MCP session owner. Logout through `/oidc/logout`
or the existing `/session/logout` revokes that browser only. Browser mutations
require an Origin header matching the callback origin; ordinary same-origin GET
requests do not need an Origin header.

`GET /oidc/session` reports the current user ID, role, and administrative access.
Sessions expire after `session_ttl_seconds` (default one hour). Admission loss
observed at a later login, or changed role/policy claims, revokes earlier browser
sessions. Changes at the provider are not pushed to LiteLLM: until a new login
observes them, existing sessions remain valid up to their configured lifetime.
Configuration changes and session-secret rotation require a fresh login. Restart
all replicas consistently after changing configuration or secrets.

The reserved identity snapshot in user metadata is integrity protected. Native
metadata edits cannot mint trusted claims. Policy changes also destroy old
session handles, so restoring an earlier metadata snapshot does not revive a
revoked session. The implementation reuses the existing verification-token table
with reserved, blocked records; it does not store provider bearer tokens there.
Back up the database and deployment secrets using the same controls as other
LiteLLM authentication data. Expired records may remain until the deployment's
normal database housekeeping removes them; expiry is checked on every request.

## Manage personal connections

Open `/ui/connect/` after signing in. The existing LiteLLM page shows only
servers available to your identity. Open a server and choose **Connect** to
authorize it. A saved connection survives sign-out. Use an authorized MCP client
with your provider access token to run tools; the personal page does not execute
tools using the browser session.

**Reconnect needed** means the provider rejected the saved authorization. Choose
**Reconnect** to authorize again. **Disconnect** removes the saved credentials
after confirmation; a failed request leaves the connection unchanged in the
page. Provider revocation is attempted separately and may be unsupported or fail.

A **Client-managed** server uses authorization supplied by the MCP client. The
page neither stores that authorization nor offers a misleading disconnect action.
**Setup needed** requires an administrator to configure the upstream OAuth client
before personal authorization can start. **Refresh connections** retries loading
after a network failure. An expired LiteLLM session returns to sign-in.

The page supports phone layouts and keyboard controls. Direct administration
navigation checks the server session before rendering management content; protected
API endpoints independently enforce permissions. Managed token exchange returns
connection status only. Provider and retained MCP tokens are never placed in
browser storage or the readable dashboard envelope.

## Enable provider-token API access

Configure the API resource and token profile separately from browser login:

```yaml
general_settings:
  oidc_api:
    resource: https://llm.example.org
    audience: litellm-api
    scope_claim: scope
    scope_format: space_delimited
    token_types: [at+jwt]
    scopes:
      - name: https://llm.example.org/access
        token_value: access
    required_claims:
      - claim: identity_kind
        any_of: [user]
```

`oidc_api` uses the `oidc_identity` issuer, stable subject, admission rule and
ordered role mappings. The [APISettings schema](../upstream/litellm/litellm/proxy/auth/oidc_api.py)
is the configuration reference. `resource` is the public proxy origin, without a
path. Sub-path deployments are not supported by this metadata configuration.
Restart all replicas after changing configuration.

`scopes[].name` is the scope a client requests and sees in discovery;
`token_value` is the value required in the validated token. All listed scopes
are required. For Entra v2 API tokens, configure the API application's client ID
as `audience`, `scope_claim: scp`, and `token_types: [JWT]`. For example, an
advertised `api://<api-application-id>/access` can map to `token_value: access`.
For providers with an array scope claim, use `scope_format: array`.

The default `at+jwt` profile follows the access-token type marker in
[RFC 9068](https://www.rfc-editor.org/rfc/rfc9068.html). Entra uses the generic
`JWT` type and documents `scp` as delegated user permissions in its
[access-token reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference).
For generic `JWT`, the API audience must differ from the browser client ID.
Strict `at+jwt`/`application/at+jwt` profiles may share that audience because
the signed token type distinguishes access tokens. Multi-audience tokens must
include the configured API audience; generic `JWT` tokens must not also include
the browser client ID. In either case, use permissions that the
provider grants only to users. `required_claims` can require additional
provider-specific user markers; the `identity_kind` example is a custom claim,
not an OIDC standard. There is no universal JWT claim identifying a human.
The explicit Entra `idtyp: app` value is rejected; its absence alone does not
prove user identity. Configure the issuer to distinguish user grants from
client-credentials grants before enabling personal connections.

Clients send `Authorization: Bearer <provider-access-token>`. RS256/ES256
signatures, exact issuer/audience, expiry, issued-at time, configured scopes,
claim rules and stable identity are checked on each request. ID tokens, tokens
for another resource, and tokens carrying an ID-token nonce are rejected.
Opaque token introspection is not implemented. Provider discovery and JWKS
must be reachable; verification fails closed if they are unavailable.

An API-only first visit creates a minimal account. Each API request gets its
own role from the current verified token; it cannot inherit a broader persisted
browser role. API grants do not replace browser policy snapshots, elevate the
persisted role, or revoke an independently valid browser grant when narrower
claims are presented. Browser sign-in remains responsible for its persisted
role and session revocation. Use the same immutable subject claim across the
two registrations, even when their `sub` values differ. Native keys therefore
do not automatically inherit elevated rights from API-only account creation.

The existing root and aggregate MCP protected-resource endpoints advertise the
configured external issuer and scope names:

- `/.well-known/oauth-protected-resource`
- `/.well-known/oauth-protected-resource/mcp`

They follow [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728.html), use the
configured public origin rather than the request's Host header, and remain
public. API authentication/scope failures carry a Bearer challenge. Clients
follow the advertised issuer's discovery to its provider endpoints; LiteLLM
does not issue provider tokens or publish role values as OAuth scopes.
Existing per-server upstream discovery remains separate.

## Keep personal MCP credentials separate

With `oidc_api` configured, personal credential list/status/storage/removal and
personal OAuth session/authorize/register/token endpoints require a verified
provider user token or the secured OIDC browser session. Native keys, including
the master key, cannot select stored per-user OAuth/BYOK credentials for MCP
calls. Native model API access, MCP server administration, and non-personal
server credentials remain available. Per-server administrative credential
inventory is still an administration operation.

For OAuth Delegate, send the LiteLLM provider token in `Authorization` and the
separate upstream credential in `x-mcp-<server-alias>-authorization`. The
existing alternative is `x-litellm-api-key: <provider-token>` with the upstream
token in `Authorization`. The admission credential is removed from MCP egress;
only the separate server credential reaches the upstream. Personal stored
connections use the stable user identity, not the browser's email or a
client-supplied user ID.

The API verifies JWTs locally against provider keys; it does not receive live
revocation events or introspect each grant. An already issued token can remain
valid until expiry despite a provider-side role change or revocation. Browser
sessions have their separate lifetime described above. Choose bounded token
and session lifetimes for the deployment. Real Entra/other-provider client
compatibility still requires testing the chosen tenant and registrations.

## Configure personal MCP connections

Choose the existing connection model on each centrally configured server. For
example, add these server definitions and extend `oidc_identity`:

```yaml
mcp_servers:
  reports:
    server_id: reports
    url: https://reports.example.org/mcp
    transport: http
    auth_type: oauth2
    oauth2_flow: authorization_code
    issuer: https://reports-id.example.org
    client_id: litellm-reports
  catalog:
    server_id: catalog
    url: https://catalog.example.org/mcp
    transport: http
    auth_type: oauth_delegate

general_settings:
  oidc_identity:
    # Keep the provider settings shown above.
    mcp_access_rules:
      reports: {claim: roles, any_of: [reports-user]}
      catalog: {claim: groups, any_of: [catalog-users]}
```

Keys in `mcp_access_rules` identify a server ID, alias, or configured name, in
that precedence order. Pin `server_id` for stable configuration across updates.
A matching rule grants personal availability without requiring a native API key.
A nonmatching rule denies visibility, connection and tool access, including for
an administrator using their own connection. Existing explicit user, team,
organization, tool and network restrictions still apply. Native user server
entitlements combine direct server grants, tool permissions, toolsets and access
groups; tool restrictions and explicit MCP opt-out remain binding. Servers without a rule
retain their existing LiteLLM permissions. Rule claims are retained automatically;
API requests use their current verified token claims, never the browser's saved
role. Configuration changes require restarting the proxy.

For managed OAuth, configure a registered upstream client ID and any required
client secret using LiteLLM's existing server configuration. Managed per-user
dynamic client registration is not supported; this limitation concerns the MCP
client, not the OIDC login provider. A missing client ID produces the status
`unavailable` with `configuration_error: oauth_client_required`. Client-supplied
and other existing DCR modes retain their upstream behavior. Register the proxy's `/callback` URL with the
upstream authorization server. The authenticated personal flow uses the existing
`/v1/mcp/server/oauth/{server_id}/authorize` and `/token` endpoints with S256 PKCE;
the browser returns through `/ui/mcp/oauth/callback`. The code binds the verified
user, server, client redirect URI and verifier. Alias/root token routes cannot
redeem this managed flow anonymously. The managed exchange stores credentials
server-side and returns connection status without access or refresh tokens.
The existing UI integration is being adapted to this response in issue #7.

Connections use the existing encrypted per-user, per-server database records.
Keep `LITELLM_SALT_KEY` stable and protected together with database backups, as
required by the existing credential encryption. Logout and proxy restart retain
managed connections. Browser sessions may connect, inspect and disconnect;
executing personal MCP tools requires the user's provider access token. Native
LiteLLM keys cannot select these personal credentials. Neither model falls back
to another user's connection.

The credential status endpoint reports `connection_mode`, `connection_status`,
`can_refresh`, and expiry without exposing tokens. Expired grants with a usable
refresh token remain refreshable. A temporary refresh-service failure denies the
current call but permits a later retry. An upstream authentication rejection triggers
one coordinated refresh and one request retry, including when expiry metadata was
not supplied. An invalid refresh grant or rejection of the renewed token requires
reconnecting; successful authorization replaces that user's old grant.
Concurrent refresh cannot recreate a disconnected record. Revocation at the
upstream is detected on use, not pushed to LiteLLM.

`DELETE /v1/mcp/server/{server_id}/oauth-user-credential` removes the local
credential and invalidates its caches first. It then attempts the issuer's
advertised revocation endpoint and reports `upstream_revocation` as `revoked`,
`unsupported`, `failed`, or `no_credential`. `revoked` means the upstream
endpoint accepted the revocation request; it is not independent verification
that every upstream session has ended. A remote failure does not restore the
local connection. An already running upstream operation is not cancelled.
Users may remove their own retained connection after losing server permission;
this does not restore visibility or execution access.

OAuth Delegate uses the separate upstream header described above. It never saves
a managed connection, and the managed credential-write endpoint rejects this
mode. Clients obtain and refresh their upstream credentials themselves. This
release does not require Entra OBO or turn the gateway into an identity provider.

## Verify an image

```sh
docker build -t litellm-noenterprise:identity .
python3 scripts/test_identity.py litellm-noenterprise:identity
python3 scripts/test_identity.py litellm-noenterprise:identity --reject-missing-master
python3 scripts/test_identity.py litellm-noenterprise:identity --test-pattern test_scope_profile.py --api-scope-name https://api.example.test/invoke --api-token-scope invoke --api-audience litellm-test --api-token-type at+jwt
python3 scripts/test_identity.py litellm-noenterprise:identity --api-audience litellm-test --reject-invalid-api
```

The runner starts isolated PostgreSQL, issuer, MCP, and real proxy containers.
It exercises public HTTP login, persisted account/role updates, logout,
protocol failures, metadata tampering/replay, provider-token admission, scope
discovery, independent browser/API grants, native-key compatibility, MCP token
separation, and cross-user MCP session isolation. It uses installed image code
by default; `--source-overlay` is an explicit development option. No live provider credentials are needed. These
checks also cover managed OAuth binding, both credential models, claim-based
server permissions, refresh/reconnect, logout retention, disconnect races, and
cold-restart persistence. They do not establish live provider compatibility.

Validation on 2026-11-01 passed 43 public HTTP/MCP journeys against the installed
ARM64 image, followed by a proxy restart and stored-connection verification.
The suite includes two identities, direct and virtual tool authorization,
managed OAuth binding, transient and permanent refresh failures, disconnect
during refresh, access-token expiry without metadata, bounded recovery from
upstream rejection, and token-free connection responses.
Proxy logs contained none of the fixture credential sentinels. Source and image
audits and the baseline startup/completion checks also passed.

### Run browser acceptance

Install the existing pinned UI test dependencies and Chromium, then run the
installed image with isolated fixtures:

```sh
npm ci --prefix upstream/litellm/tests/e2e/ui --ignore-scripts
upstream/litellm/tests/e2e/ui/node_modules/.bin/playwright install --with-deps chromium
python3 scripts/test_identity.py litellm-noenterprise:1.106.6 --browser
python3 scripts/test_identity.py litellm-noenterprise:1.106.6 --browser --browser-admin-disabled
```

These runs require free loopback ports 4000, 8080 and 8090. The dedicated browser
maps fixture hostnames locally; no system DNS configuration changes. On macOS,
`UI_BROWSER_CHANNEL=chrome` uses installed Chrome. Set `UI_EVIDENCE_DIR` to an
absolute path outside a disposable checkout to retain sanitized screenshots.
The runner removes its containers and network when it finishes.

Browser journeys cover real redirects, fallback permissions, managed connect,
provider-token tool use, sign-out retention, reconnect, disconnect and its network
failure, client-managed guidance, missing operator setup, expired sessions, direct
admin navigation, and desktop/phone keyboard access. Browser token leakage checks
inspect response bodies, web storage and cookie accessibility without printing
credentials. Both administration-visibility profiles passed on 2026-11-01: 20
desktop/phone journeys against the installed ARM64 image. Live Entra and
third-party MCP interoperability remain unverified.

The consumed personal status type is generated from the backend model. From the
dashboard directory, run `npm run gen:api -- --personal-mcp` with `LITELLM_PYTHON`
pointing to the installed distribution's Python interpreter. This produces
`src/lib/http/personal-mcp-schema.d.ts`; do not edit it manually. The scoped export
keeps unrelated upstream dashboard API bindings unchanged.
