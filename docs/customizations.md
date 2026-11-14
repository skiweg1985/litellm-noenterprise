# Changes from upstream LiteLLM

This register lists every deliberate difference between this distribution and
upstream LiteLLM 1.106.6. Use it to understand what the image adds, and to
check each change again when updating to a new upstream release
([update procedure](upstream.md#update-to-a-selected-release)).

Usage and configuration live in the feature guides:
[identity and personal MCP](identity.md) and
[provider extensions](extensions.md). This page records what changed,
where it lives, and which pull request introduced it.

## How changes are kept separate

- The `vendor/litellm` branch holds the reviewed original upstream bytes.
  `upstream/manifest.json` records each file's hash
  ([reviewed source](upstream.md#reviewed-source)).
- New behavior lives in new modules where possible, mainly under
  `litellm/proxy/extensions/`. Upstream files receive narrow hooks that call
  into these modules.
- Changes to upstream files are ordinary commits on `main`. They are not
  vendor provenance and are not checked against the import manifest.

List the current differences, excluding tests:

```sh
git fetch origin refs/heads/vendor/litellm:refs/remotes/origin/vendor/litellm
git diff --name-status origin/vendor/litellm origin/main -- upstream/litellm \
  | grep -vE '\.test\.|/tests?/|test_'
```

On 2026-11-14 this showed 80 modified and 79 added upstream files, without
test files.

## Overview

| Change | Pull requests | Guide |
| --- | --- | --- |
| [Enterprise-free source selection and image](#enterprise-free-distribution) | #8 | [upstream.md](upstream.md) |
| [OIDC browser identity and provider-token API access](#oidc-identity) | #8 | [identity.md](identity.md) |
| [OIDC setup in SSO Settings, account linking, Entra preset](#oidc-settings-and-account-linking) | #28, #31, #33 | [identity.md](identity.md#configure-in-the-dashboard) |
| [Personal MCP connections](#personal-mcp-connections) | #8 | [identity.md](identity.md#manage-personal-connections) |
| [Provider compatibility fixes](#provider-compatibility-fixes) | #16 | [extensions.md](extensions.md#provider-behavior) |
| [ChatGPT subscriptions](#chatgpt-subscriptions) | #16, #24 | [extensions.md](extensions.md#manage-chatgpt-subscriptions) |
| [OpenRouter image generation](#openrouter-image-generation) | #26 | [extensions.md](extensions.md#generate-images-with-openrouter) |
| [GPT-Live voice sessions on Azure](#gpt-live-on-azure) | #35, #37 | [extensions.md](extensions.md#talk-to-gpt-live-on-azure) |
| [Azure Realtime Translate](#azure-realtime-translate) | #38, #40 | [extensions.md](extensions.md#translate-speech-with-azure-realtime-translate) |
| [Nova Sonic voice sessions on Bedrock](#nova-sonic-on-bedrock) | #45 | [extensions.md](extensions.md#talk-to-nova-sonic-on-bedrock) |
| [Dashboard fixes](#dashboard-fixes) | #41 | — |

Pull request numbers refer to
[skiweg1985/litellm-noenterprise](https://github.com/skiweg1985/litellm-noenterprise/pulls?q=is%3Apr+is%3Amerged).

## Enterprise-free distribution

Introduced in #8.

- The import excludes `enterprise/`, the prebuilt dashboard, and the
  Apache-2.0 NVIDIA capability classifier
  ([exclusions](upstream.md#reviewed-source)).
- The dashboard is rebuilt from source in the `Dockerfile`.
- `router_strategy/complexity_router/config.py` and `complexity_router.py`
  reject `classifier_type: capability` with a clear error, because the
  classifier is not part of the MIT-only selection.
- Repository tooling outside `upstream/`: `scripts/import_upstream.py`,
  `scripts/audit_source.py`, `scripts/audit_image.py`,
  `scripts/smoke_image.py`, `runtime/` (Python packaging, locked
  requirements, license supplements).

## OIDC identity

Introduced in #8. Independent of LiteLLM's enterprise SSO.

New modules:

- `litellm/proxy/auth/oidc_identity.py`: browser sign-in, sessions, stable
  accounts, claim-based admission and ordered role mapping.
- `litellm/proxy/auth/oidc_api.py`: provider-token API admission and
  discovery metadata.

Hooks in upstream files:

- `proxy/auth/user_api_key_auth.py`, `proxy/auth/auth_exception_handler.py`:
  accept OIDC sessions and provider tokens next to virtual keys.
- `proxy/management_endpoints/ui_sso.py`, `session_endpoints.py`,
  `repositories/verification_token_repository.py`: session handling.
- `proxy/proxy_server.py`: route registration.
- Dashboard: `hooks/useOidcSession.ts`, `app/(dashboard)/layout.tsx`,
  `hooks/useAuthorized.ts`, `components/navbar.tsx`.

## OIDC settings and account linking

Introduced in #28 (settings), #31 (linking) and #33 (Entra preset).

- `proxy/auth/oidc_settings.py`: OIDC configuration stored in the database
  and edited in **Admin Settings → SSO Settings**, applied to all proxy
  workers. Deployment-file configuration remains supported.
- `proxy/auth/oidc_links.py`: links a new OIDC identity to an existing
  LiteLLM user after administrator review. User IDs, keys, teams and spend
  history stay unchanged.
- Dashboard: `components/Settings/AdminSettings/SSOSettings/oidc/`
  (settings form and Microsoft Entra ID preset) and
  `app/(dashboard)/users/_components/oidc/OIDCAccountLinks.tsx`.
- Hooks: `proxy/auth/oidc_identity.py`, `oidc_api.py`,
  `user_api_key_auth.py`, `proxy_server.py`,
  `discovery_endpoints/ui_discovery_endpoints.py`,
  `management_endpoints/internal_user_endpoints.py`.

## Personal MCP connections

Introduced in #8. Builds on LiteLLM's existing managed OAuth and
client-supplied MCP modes.

- New: `proxy/_experimental/mcp_server/oidc_policy.py` (per-server claim
  rules).
- Modified MCP modules: `db.py`, `discoverable_endpoints.py`,
  `mcp_server_manager.py`, `oauth_utils.py`, `operations.py`,
  `server_resolution.py`, `upstream.py`,
  `auth/user_api_key_auth_mcp.py`, the three `outbound_credentials/` modules,
  `management_endpoints/mcp_management_endpoints.py` and
  `types/mcp_server/mcp_server_manager.py`. They add protected persistent
  per-user credentials, refresh and disconnect, and keep native tool and
  toolset grants.
- Dashboard: `/ui/connect/` (`app/connect/`), the MCP OAuth callback page,
  `hooks/useUserMcpOAuthFlow.tsx` and `components/chat/MCPAppsPanel.tsx` work
  for users without administration access.

## Provider compatibility fixes

Introduced in #16.

- ChatGPT: accepts the standard client request shape, handles SSE without a
  content type, and routes Chat Completions through the Responses backend
  (`llms/chatgpt/chat/transformation.py`,
  `llms/chatgpt/responses/transformation.py`, `main.py`,
  `completion_extras/litellm_responses_transformation/transformation.py`).
- Parallel streamed tool calls keep their arguments on the right call
  (`llms/custom_httpx/llm_http_handler.py`).
- MCP: a session that belongs to another caller returns a JSON-RPC
  "Session not found" with HTTP 404 instead of 403, so clients reinitialize
  after a token refresh (`proxy/_experimental/mcp_server/server.py`).
- Optional OpenRouter Jev classifier for the complexity router, off by
  default (`router_strategy/complexity_router/openrouter_jev.py`).

## ChatGPT subscriptions

Introduced in #16, extended in #24.

New package `litellm/proxy/extensions/chatgpt_subscriptions/`: account
registry and lifecycle, device sign-in, token storage, fixed assignment,
ordered reserves, distribution, bulk assignment, per-attempt attribution,
observed usage and quota monitoring. Shared types are in
`types/proxy/subscription_attribution.py`.

Hooks in upstream files:

- Routing: `router.py`, `proxy/route_llm_request.py`,
  `proxy/litellm_pre_call_utils.py`.
- Provider transport: `llms/chatgpt/authenticator.py`, `common_utils.py`,
  `responses/transformation.py`, `llms/custom_httpx/llm_http_handler.py`.
- Logging and spend: `litellm_core_utils/litellm_logging.py`,
  `responses/streaming_iterator.py`, `proxy/hooks/proxy_track_cost_callback.py`,
  `proxy/common_request_processing.py`, `proxy/utils.py`,
  `proxy/spend_tracking/spend_management_endpoints.py`,
  `spend_tracking_utils.py`, `types/utils.py`, `proxy/_types.py`.
- Model management: `proxy/management_endpoints/model_management_endpoints.py`.
- Dashboard: **ChatGPT subscriptions** page and usage page
  (`app/(dashboard)/chatgpt-subscriptions/`), `components/subscriptions/`,
  subscription fields in the native model forms (`AddModelForm.tsx`,
  `ModelInfoEditForm.tsx`, `model_info_view.tsx`, `handle_add_model_submit.tsx`),
  subscription columns, filters and attempt details in request logs
  (`components/logs/`), navigation entries (`leftnav.tsx`,
  `page_metadata.ts`), API types (`scripts/gen-api-types.mjs`, `lib/http/`).

## OpenRouter image generation

Introduced in #26.

- New: `llms/openrouter/image_generation/native_transformation.py` for
  OpenRouter's `/api/v1/images` API.
- Modified: `llms/openrouter/image_generation/__init__.py`,
  `transformation.py`, `images/main.py`, `utils.py` (route selection).
- Dashboard: SVG responses render in the Playground
  (`playground/llm_calls/image_generation.tsx`).

## GPT-Live on Azure

Introduced in #35 (WebSocket, client delegation), extended in #37 (WebRTC).

- New package `litellm/proxy/extensions/gpt_live/`: routes for
  `/v1/live/sessions`, relay, WebRTC, policy and accounting.
- Hooks: `llms/azure/realtime/handler.py`, `proxy/_types.py` (route
  classification), `proxy/proxy_server.py` (registration).
- `GPT_LIVE_MAX_SESSION_SECONDS` limits a session (default 1800).

## Azure Realtime Translate

Introduced in #38. #40 skips spend logs for sessions without audio.

- New package `litellm/proxy/extensions/realtime_translate/`:
  `/v1/realtime/translations` route, stream handling, policy and accounting.
- Hooks: `llms/azure/realtime/handler.py`, `realtime_api/main.py`
  (translation models are refused on `/v1/realtime`), `proxy/_types.py`,
  `proxy/proxy_server.py`.
- `LITELLM_TRANSLATE_MAX_SECONDS` limits a session (default 600, at most
  3600).

## Nova Sonic on Bedrock

Introduced in #45 (spec #44).

- New package `litellm/proxy/extensions/nova_sonic/`: `/v1/sonic/sessions`
  route, relay of Bedrock's native events, policy and token accounting.
- Hooks: `llms/bedrock/realtime/handler.py` (marked sessions go to the relay
  after credentials are resolved), `proxy/_types.py`, `proxy/proxy_server.py`.
- Dependency: `scripts/runtime_requirements.py` adds upstream's
  `bedrock-realtime` extra (`aws-sdk-bedrock-runtime[awscrt]`) to
  `runtime/requirements.in` and the lock. This also enables upstream's
  `/v1/realtime` for Bedrock models.

## Dashboard fixes

- #41: tables with `fillHeight` (`components/shared/DataTable/DataTable.tsx`)
  fill the viewport only from the `md` breakpoint. On phones, the toolbar,
  banners and pagination collapsed the rows behind the sticky header; there the
  table now keeps its natural height and the page scrolls.

## Planned removals

- [#42](https://github.com/skiweg1985/litellm-noenterprise/issues/42):
  remove the upstream upgrade banner and its unauthenticated request to the
  GitHub releases API. It points to BerriAI releases that this distribution
  does not install directly.

## Update checklist

The [update procedure](upstream.md#update-to-a-selected-release) merges the
new `vendor/litellm` snapshot into `main`. Git carries these changes forward;
conflicts appear where upstream changed the same lines. For each update:

1. Run the diff command above before and after the merge, and compare both
   with this register.
2. Resolve conflicts with this register at hand. Conflicts are most likely in
   `proxy_server.py`, `proxy/_types.py`, `router.py`, `llm_http_handler.py`,
   `user_api_key_auth.py` and the dashboard model forms.
3. Check whether upstream now provides a change natively, and drop ours if so.
4. Update this register in the same pull request.

Any pull request that changes a file under `upstream/litellm` updates this
register.
