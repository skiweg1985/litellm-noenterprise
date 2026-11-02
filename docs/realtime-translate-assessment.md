# Azure Realtime Translate feasibility

Assessed on 2026-11-02 against application revision
`b67e50079a598bea134251c7ca8903dda200843a`, based on LiteLLM 1.106.6.
Translation is not implemented in this application.

## Proposed behavior

Expose `/v1/realtime/translations?model=<public-alias>` through existing proxy
authentication, model authorization and router workflows. Keep Azure credentials
on the server. Support continuous translated audio and text, with optional source
transcription and noise reduction. Record proxy-measured audio duration and
calculated costs through ordinary logs.

Microsoft documents the Azure upstream `/openai/v1/realtime/translations`
endpoint in its [WebSocket guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/realtime-audio-websockets#translate-audio-in-real-time).
The [translation event reference](https://developers.openai.com/api/reference/resources/realtime/translation-client-events)
defines audio appends, language/input updates and graceful session close.
Confirm supported options against Azure before claiming compatibility.

## Integration points

- Reuse native WebSocket authentication, resolved-model authorization and
  pre-call/router processing. Classify the new route for OIDC API admission.
  Sources: [authentication](../upstream/litellm/litellm/proxy/auth/user_api_key_auth.py#L679),
  [native route](../upstream/litellm/litellm/proxy/proxy_server.py#L13264),
  [OIDC admission](../upstream/litellm/litellm/proxy/auth/oidc_api.py#L131).
- Add translation-specific Azure transport without ordinary Realtime turn
  transformations. Preserve credential resolution, TLS verification and
  intentional proxy behavior. Support named credentials and model aliases.
  Source: [Azure handler](../upstream/litellm/litellm/llms/azure/realtime/handler.py#L143).
- Assign one owner to terminal logs, budget reservations and parallel-slot
  cleanup on success, rejection, timeout, disconnect and cancellation.
  Source: [native lifecycle](../upstream/litellm/litellm/proxy/proxy_server.py#L13225).
- Include the extension in the independent source package and container build.
  Preserve vendor provenance and Enterprise exclusions. Sources:
  [packaging](../runtime/pyproject.toml#L15), [build](../Dockerfile),
  [source policy](upstream.md#reviewed-source).
- Make session limits and per-second pricing configurable. Distinguish measured
  proxy audio and calculated spend from provider-reported usage. Document
  embedded-transcription billing coverage and concurrency enforcement scope.

## Effort and acceptance

Estimate for one developer, assuming an available Azure deployment:

| Scope | Estimated effort |
| --- | --- |
| Experimental API implementation and controlled relay demonstration | A few hours |
| Reviewed API integration, regression tests, documentation and live acceptance | 2–3 working days total |
| New dashboard microphone/translation interface | Additional 2–4 working days |

These are planning estimates, not measured implementation times. WebRTC,
additional providers and distributed concurrency coordination require separate
scope. No installed implementation or current live Azure compatibility has been
verified for this feature.

Use the installed application's public WebSocket API with a controlled upstream
as the main test boundary. Cover streaming before input finishes, credentials,
authorization, limits, output draining, cancellation and exactly one accounting
outcome. Reuse native Realtime/Transcribe regressions and existing source/image
audits. A separate authorized Azure acceptance with synthetic audio is required
before claiming live compatibility. A new dashboard interface is separate work.
