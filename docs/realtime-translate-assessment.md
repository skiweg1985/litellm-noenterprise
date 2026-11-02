# Realtime Translate integration assessment

Assessed on 2026-11-02 against application revision
`b67e50079a598bea134251c7ca8903dda200843a` (LiteLLM 1.106.6) and
[deployment branch revision 513cca3](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25).
Translation is not implemented in this application. This assessment adds no
runtime behavior and does not establish current Azure compatibility.

## Recommendation and effort

Port the existing extension into the application's source tree. The feature is
small enough to reuse, but the old installer cannot be added unchanged.
Keep the independent container build and vendor ancestry intact.

Engineering estimate for one developer, assuming an available Azure deployment:

| Scope | Estimated effort |
| --- | --- |
| Experimental API port and local relay demonstration | A few hours |
| Reviewed API integration, lifecycle/accounting regression tests, documentation and live acceptance | 2–3 working days total |
| New dashboard microphone/translation interface | Additional 2–4 working days, depending on scope |

These are estimates, not measured implementation times. Distributed session
limits, WebRTC and additional providers require separate scope. The estimate
assumes the existing standalone WebSocket client, not a new browser product.

## What the branch implements

The extension comprises six Python modules (757 lines) and a 207-line installer:

- A dedicated `/v1/realtime/translations?model=<public-alias>` WebSocket route
  reuses native key authentication, model authorization, pre-call processing
  and router dispatch. Azure credentials remain server-side; client destination
  and credential overrides are rejected. Retries and fallbacks are disabled.
- The Azure relay connects to `/openai/v1/realtime/translations`, forwarding
  continuous base64 PCM16 mono audio at 24 kHz and translated audio/text events.
  Optional input transcription and noise reduction are supported. It bypasses
  ordinary Realtime turn transformations and redacts provider failures.
- Session defaults are 600 seconds and one session per key, configurable up to
  3600 seconds and 32 sessions. File locks coordinate workers sharing the same
  local directory; they do not provide a distributed replica limit.
- Accounting calculates input duration from successfully forwarded PCM bytes,
  applies a configurable per-second price, and dispatches native success/failure
  logging. Partial failures retain calculated usage; audio and transcript
  content are not included in its accounting summary. Embedded transcription
  billing is not separately implemented.
- Translation models are blocked on ordinary Realtime WebSocket and HTTP
  client-secret, transcription-session and calls paths.

Sources: [routes](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/extensions/realtime_translate/routes.py),
[policy](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/extensions/realtime_translate/policy.py),
[stream](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/extensions/realtime_translate/stream.py),
[limits](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/extensions/realtime_translate/limits.py),
[accounting](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/extensions/realtime_translate/accounting.py),
[installer](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/patches/patch_realtime_translate.py).
Microsoft documents the same dedicated Azure translation endpoint in its
[WebSocket guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/realtime-audio-websockets#translate-audio-in-real-time).

## Required adaptation

1. **Replace installation with source integration.** The
   [manifest](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/patches/realtime_translate_base.json)
   pins 1.101.0-rc.1 and complete hashes for five files. All five differ here;
   the unchanged `--check` rejects the first file. All eight replacement anchors
   remain unique and their in-memory transformations parse, indicating limited
   structural drift, not proven runtime compatibility. Package the modules under
   `litellm`, since [package discovery](../runtime/pyproject.toml#L15) only includes
   `litellm` and `litellm.*`. The old
   [Dockerfile](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/Dockerfile)
   uses an upstream binary image and must not replace our independent build.
2. **Preserve native authentication and lifecycle ownership.** Register the
   translation path in [openai_routes](../upstream/litellm/litellm/proxy/_types.py#L436)
   so [OIDC route admission](../upstream/litellm/litellm/proxy/auth/oidc_api.py#L131)
   recognizes it. Reuse
   [WebSocket authentication](../upstream/litellm/litellm/proxy/auth/user_api_key_auth.py#L679),
   resolved-model authorization and [pre-call/router processing](../upstream/litellm/litellm/proxy/proxy_server.py#L13264).
   The old extension already shields cleanup and tracks callback dispatch;
   reconcile it with current reservation/parallel-slot release behavior rather
   than assuming either path can be copied unchanged. Test exactly one terminal
   log and release on rejection, timeout, cancellation and disconnect.
3. **Support native configuration.** The old target validator requires explicit
   Azure origin and credential fields before router credential resolution.
   A deployment configured only with `litellm_credential_name` fails that check;
   resolve named credentials safely before target validation. Review the custom
   relay's fresh TLS context and `proxy=None` against the
   [current Azure handler](../upstream/litellm/litellm/llms/azure/realtime/handler.py#L143).
   Retain HTTP/ordinary-route guards and native model aliases. The existing
   [container permissions](../Dockerfile) permit the default lock directory;
   replicas still need an explicit concurrency policy.
4. **Review protocol and pricing drift.** Current
   [translation event documentation](https://developers.openai.com/api/reference/resources/realtime/translation-client-events)
   includes `session.close` to flush pending output, optional noise-reduction
   `strength`, and event IDs up to 512 characters. The old policy rejects the
   first two and limits IDs to 128. Confirm Azure support before extending it;
   cover graceful output draining. The bundled client instead waits a fixed
   drain period. Its hard-coded price default is not tenant billing evidence;
   configure deployment pricing and document transcription-cost coverage.

## Evidence and remaining acceptance

Checks performed on the downloaded, pinned source without modifying application
runtime code:

- Policy/route/relay suite: 23 tests, 22 passed and one installer test skipped
  because no disposable copy of the old pinned RC was supplied.
- Example-client suite: one test passed.
- Accounting/configuration suite: nine tests passed in an isolated container
  using this checkout's Python source and an existing 1.106.6 image's dependencies.
- Installer check against this checkout: rejected the version mismatch as
  expected. All eight anchors and the four transformed Python files passed
  structural/syntax probes only.

These 32 passing tests include mocks and local fixtures. The native integration
suite was inspected but not run against an installed port, because no port was
implemented. The deployment branch's
[historical validation](https://gitea.simplicity.ag/b.shirley/litellm-deployment/src/commit/513cca3dafb1dc182faab5b7ba73585401d96e25/docs/realtime-translate-validation.md)
reports live translation, embedded transcription and spend checks on the old RC;
it is not fresh evidence for this app or the current service.

Before release, run installed-image translation tests and unchanged native
Realtime/Transcribe regressions, OIDC/key/model/route admission, named credentials,
concurrent limits and cleanup, and partial-failure database accounting. Run the
existing [source/image audits](upstream.md#build-and-check-the-image), then a live
Azure acceptance with synthetic audio and authorized credentials. No new
Playground interface is supplied by the source patch.
