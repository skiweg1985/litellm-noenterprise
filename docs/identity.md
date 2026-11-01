# Configure user identity

The independent OIDC integration uses the existing LiteLLM user database and
personal connection page. It supports independent browser and provider-token API
grants without enabling premium features. Per-server MCP access rules remain a
separate follow-up.

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

Administrators land on `/ui/`; other users land on `/ui/connect/`. Personal
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
checks do not establish live provider compatibility or the final MCP connection
workflow, which has its own acceptance work.

Validation on 2026-11-01 passed all 25 HTTP/MCP journeys against the installed
ARM64 image, without a source overlay, plus an alternate scope/type profile and
startup rejection for missing master keys and unsafe generic-JWT audiences.
The source audit and nine baseline tests passed; the image audit found no
excluded enterprise package paths in any of its 13 final layers.
