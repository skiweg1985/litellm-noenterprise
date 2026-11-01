# Configure browser identity

The independent OIDC integration uses the existing LiteLLM user database and
personal connection page. It does not enable premium features. Provider-token
API admission and per-server MCP rules are separate follow-up work.

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
this browser flow does not provide API bearer admission.

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

## Verify an image

```sh
docker build -t litellm-noenterprise:identity .
python3 scripts/test_identity.py litellm-noenterprise:identity
python3 scripts/test_identity.py litellm-noenterprise:identity --reject-missing-master
```

The runner starts isolated PostgreSQL, issuer, MCP, and real proxy containers.
It exercises public HTTP login, persisted account/role updates, logout,
protocol failures, metadata tampering/replay, and cross-user MCP session
isolation. It uses installed image code by default; `--source-overlay` is an
explicit development option. No live provider credentials are needed. These
checks do not establish live provider compatibility or the final MCP connection
workflow, which has its own acceptance work.
