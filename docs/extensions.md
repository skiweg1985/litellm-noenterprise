# Independent provider extensions

The image includes independent provider fixes and a ChatGPT subscription
administration page. These changes are separate from the reviewed vendor import
and are checked again when updating LiteLLM.

## Manage ChatGPT subscriptions

Sign in to the dashboard as a proxy administrator and open **ChatGPT
subscriptions**. The page uses the existing dashboard session. It shows account
status, quota windows, assigned deployments and the provider's model catalog.
Choose **Add subscription**, enter a display name, and start its device
sign-in. Each subscription can be renamed without changing its model selector.
Start or cancel a device authorization, refresh an existing connection, or
**Disconnect** from this page. A different account identity requires explicit confirmation
before replacing an existing connection. The legacy `/chatgpt/ui` URL redirects
to the integrated page.

These are centrally managed subscriptions, separate from personal MCP
connections. Non-administrators cannot use the subscription administration API.
The page never asks for a second admin key or exposes stored access/refresh
tokens. Device authorization codes are available only to administrators.

The dashboard can add subscriptions without server configuration changes.
`CHATGPT_ACCOUNTS` still imports legacy aliases such as `default,account-b`;
without this setting, one `default` account is available. Imported aliases remain
in the durable registry even if removed from this environment setting. Existing
selectors and token paths remain unchanged. New accounts receive generated
aliases; their editable display names are separate from immutable IDs.

**Disconnect** removes credentials and stops further requests until sign-in.
**Retire** also prevents reconnection and retains a read-only historical entry.
Both confirmations show the currently assigned models. Retirement does not
silently reassign those models. A request that has already obtained credentials
may finish; these controls do not cancel an in-flight provider request or revoke
the provider's grant remotely. Retired entries remain visible under **Retired
subscriptions**.

Each account keeps an opaque connection revision. Confirming a different provider
identity changes the revision; token renewal and display-name changes do not.
Disconnecting preserves the historical revision. The registry and token files
must be backed up together.
The image sets `CHATGPT_TOKEN_DIR=/var/lib/litellm/chatgpt`. Mount a persistent
volume there; the container runs as UID 10001 and must be able to write it.
Token files use restricted filesystem permissions and contain credentials.
Protect that volume and its backups. This is file-backed storage, distinct
from the encrypted database storage used for personal MCP credentials.

New device authorizations do not start automatically. Set `CHATGPT_AUTO_LOGIN=true`
only if unattended device-authorization initiation is desired. Existing signed-in
accounts refresh in the background. Starting the unconfigured image does not
connect a ChatGPT account or consume subscription quota.

To publish a model in the dashboard, choose **Add model** beside an available
model on the subscription card. The native model form preselects the subscription
and model; choose a unique public model name and save. Alternatively, select
**ChatGPT Subscription** in **Models + Endpoints → Add Model**, then choose a
connected subscription and its offered model. Existing database models can change
their subscription in the model edit form. Configuration-backed models display
their assignment but remain read-only.

The server checks the selected subscription and current model catalog when saving.
If the catalog is unavailable, retry after refreshing the connection. Existing
models remain visible. A fixed assignment cannot fall back to another model or
use a caller-supplied subscription selector; disconnecting its account makes new
requests fail until it is connected again. Multiple deployments cannot share a
fixed-assignment public name. Changes use the existing database model storage and
survive proxy restarts.

For reserves, choose **Preferred subscription with reserves** in the native model
form. Add compatible subscriptions and arrange them in preference order. Each
model keeps its own assignment; the first eligible subscription handles the
request. Up to sixteen distinct subscriptions can belong to one model. The model
remains one editable database record and one public model name. Native model
RPM/TPM limits, when enforcement is enabled, and the model concurrency limit remain
shared across its members. Adding subscriptions does not multiply those limits.
Pause and resume apply to the whole model on every worker. Native cache and
process-local semaphore semantics remain unchanged.

Rate limits, temporary provider failures and rejected credentials can move a
request to another assigned subscription, at most once per member. Invalid client
requests do not consume the remaining reserves. Provider `Retry-After` and the
model's cooldown are both respected. Rejected credentials stay unavailable until
renewed; request handling never starts a sign-in. Expired credentials that can be
refreshed remain eligible. If no member is eligible, reconnect an account or wait
for its cooldown. Native cooldown sharing between workers uses the configured
Redis cache; without Redis, cooldown memory remains local to each worker.
The model details show connection and cooldown eligibility for the saved
assignment, with a refresh action and the observation's cache scope. A connected
account is not a guarantee that a request can run; request limits are checked at
use. Disconnect and retire confirmations list every assigned model, including
models where the account is a reserve or distribution member.

The existing stream boundary is preserved. An initial HTTP failure may select a
reserve before streaming starts. Once lifecycle frames or generated content have
been forwarded, an interrupted stream ends without appending another account's
response. Cancellation never starts a reserve request. Chat Completions can buffer
lifecycle-only frames and return an error before committing its streaming HTTP
response. Logs record physical attempts, the routing mode, and actual reserve
switches. Skipped unavailable members do not become attempts.

Selection reads the current saved assignment and checks it again before provider
dispatch. Membership changes, deletion and rename therefore cannot make a stale
worker send to a removed member. The public parent ID remains the ordinary model
log filter; each attempt also records its stable member deployment ID.

After connecting an account, **Assign to models** opens an optional review of
existing ChatGPT models. Compatible reserve models append the account last;
distribution models add it to their membership. Deselect any model to leave it
unchanged. Fixed models start unselected and require an explicit replacement or
mode change. The review shows current and proposed memberships before saving.
Matching uses the upstream model ID, not the public model name. Configuration-backed
models are shown with a read-only explanation; unavailable catalogs offer retry.

Saving rechecks catalog eligibility, administrator access and each full model-row
version. Concurrent edits cause a per-model conflict instead of an overwrite.
Successful rows stay saved when another row fails. Refresh the review after a
conflict; already assigned accounts are not added twice. A failed assignment does
not disconnect the account, and signing in alone never changes model routing.

Configuration-managed deployments can still use an explicit account alias:

```yaml
credential_list:
  - credential_name: subscription-default
    credential_values:
      api_key: chatgpt-account:default
model_list:
  - model_name: subscription-model
    litellm_params:
      model: chatgpt/<model-id-from-the-account-catalog>
      litellm_credential_name: subscription-default
```

The `chatgpt-account:` value is an account selector, not a provider secret.
Missing or unknown selectors fail instead of falling back to another account.
Request logs record the subscription actually used at each provider attempt. Catalog visibility does not
automatically create a model deployment.

## Distribute model requests

Choose **Distribute requests** in a model's subscription routing settings and add
its compatible subscriptions. Each new request gives every available selected
subscription equal preference through LiteLLM's native random selection. Counts
can differ; this policy does not measure remaining quota or promise equal totals.
The choice applies to this model and leaves global routing unchanged.

The availability, cooldown and stream rules above apply to distribution too.
Recovered members can rejoin. Editing membership changes which accounts new
requests may use. The same editor also offers a fixed account or an ordered
preferred account with reserves; distribution ignores list order. Cross-member
attempts in distribution are recorded as retries rather than reserve switches.

## Find subscription requests

In **Logs**, the Subscription column shows the name recorded when each request
was sent. Open a request to see its attempts, outcomes, order, connection revision,
and observed token usage. **Filter logs for …** selects that subscription across
renames; the link preserves the selected time window. Existing log access rules
still apply.

Attribution is stored with ordinary spend logs and follows their retention policy.
It does not create extra spend records or change model names. Cache-only and older
records have no provider-attempt attribution. Missing provider usage is shown as
unknown, including interrupted streams; no token estimate is presented as observed
subscription usage. Names and connection revisions are historical snapshots and
do not expose provider account identifiers or credentials.

## Compare observed subscription usage

Open **Observed usage** from subscription management, or use the link on an
individual subscription. Choose **From (UTC)** and **Through (UTC, inclusive)**,
then **Apply period**. **View logs** keeps the subscription and exact time range;
**Connection history** separates observations by connection revision. Renamed and
retired subscriptions retain their recorded names and revisions.

The metrics come from retained request logs, independently of current model
assignments. Logical requests count distinct gateway-generated request IDs per
subscription. Provider attempts count distinct attempt IDs within those logical
requests. Failed attempts count the failure outcome; cancellation and incomplete
attempts remain visible in logs. Reserve switches count actual attempts reached
through ordered reserve routing, on the destination subscription. Distribution
retries are not reserve switches. A logical request may use several subscriptions,
so per-subscription request counts cannot be added to obtain a gateway total.

Token sums include only recorded usage and show the number of attempts without
usage. If no value was recorded, the total is **Unknown**. A partial sum is labeled
**recorded**, not presented as a complete total. These observations do not estimate
provider quota, reset windows or per-request subscription fees.

Aggregation runs in the database over the same inclusive, second-precision UTC
request-start window used by the log link. Duplicate callback snapshots count each
attempt once, preferring a terminal snapshot over an incomplete one; raw log rows
can therefore outnumber logical requests. Cache hits never create provider usage.
The coverage panel separately reports unattributed ChatGPT records and records
whose provider is unknown. Neither is assigned by parsing a model name. Known
other providers are excluded from these gap counts. The oldest retained log is
shown, but complete historical coverage remains unknown: deleted or unlogged
requests cannot be reconstructed.

The overview uses the existing native-admin/dashboard-session boundary. It exposes
no provider identities or credentials. Its generated API types are refreshed with
`npm run gen:api -- --subscription-usage` in the dashboard directory and the same
backend Python environment used for the other scoped contracts.

## Monitor subscriptions

Monitoring is disabled unless `CHATGPT_MONITORING_API_KEY` is configured.
Requests use `X-Monitoring-Key`; this key does not grant administration access.

- `GET /chatgpt/accounts`: redacted status and quota overview.
- `GET /chatgpt/accounts/{name}/usage`: account quota data.
- `GET /chatgpt/accounts/{name}/usage/prtg`: generic PRTG channel response.

Monitoring timestamps default to UTC; `CHATGPT_MONITORING_TIMEZONE` selects an
IANA timezone. Monitoring responses omit device authorization codes.

No monitoring endpoint, organization certificate, deployment address or account
credential is configured in the distributed image.

## Generate images with OpenRouter

Add an OpenRouter model with **image_generation** mode and its upstream model ID,
then select it in the Playground's image-generation view. Existing named
credentials work unchanged. Dedicated image models use OpenRouter's `/api/v1/images`
API with a text prompt; no custom API base is needed.

Known chat/image models retain their existing chat-completions route, including
OpenRouter `:variant` suffixes. Registered OpenRouter model metadata declaring both
text and image output also retains that route. Other image models use the native
Image API without a discovery request. To select a route explicitly, set the
model's API base to the complete URL ending in `/images` or `/chat/completions`.
An explicit endpoint takes precedence over automatic model selection.

Native requests preserve `size`, `quality`, and `n`: pixel sizes are not approximated
and quality is not converted into resolution. `size=auto` leaves dimensions to the
provider. Native `resolution`, `aspect_ratio`, and provider routing settings can
be supplied through `extra_body`. Unsupported adapter parameters and malformed
values fail before dispatch. Valid combinations remain model/provider-dependent;
check [OpenRouter's capability documentation](https://openrouter.ai/docs/guides/overview/multimodal/image-generation)
and the model's image endpoint metadata. Providers can reject unsupported sizes
or image counts; quality has an effect only on providers with a quality control.

The buffered response contains base64 images (`response_format=b64_json`, also the
default). Hosted-URL responses, editing/reference images, and streaming previews
are outside this adapter's scope. Image media types are preserved in
`data[].provider_specific_fields.media_type`; the Playground displays PNG, JPEG,
WebP, and SVG and retains the PNG fallback for providers without media metadata.
Provider-reported cost is retained. Token usage is reported only when the provider
supplies the counts; per-image cost is not converted into invented token usage.

## Talk to GPT-Live on Azure

GPT-Live is a full-duplex voice model. It keeps talking while background work
runs and hands that work to the client. Clients connect to
`wss://<proxy>/v1/live/sessions` with a virtual key or an OIDC API token and name
a public model in the first event:

```json
{"type": "session.start", "session": {"model": "gpt-live-1", "instructions": "Answer briefly."}}
```

Add the model like any Azure model: `azure/<deployment>` with the resource's
API base and credential. A named credential works too. The deployment must run
`gpt-live-1`. If its name differs, set `base_model: azure/gpt-live-1` in the
model info or set `input_cost_per_second`. Without a price the session is
refused. Turn off the background health check for the model, because the
Realtime health check calls a path that GPT-Live does not serve.

The proxy checks the key's model access, budget, limits and pre-call hooks
before it connects. The router picks the deployment and resolves its
credentials, which never reach the client. Azure receives the start event with
the deployment name as model. Then the proxy forwards Azure events unchanged and
accepts these client events: `session.input_audio.append`,
`session.input_audio.mute`, `session.input_audio.unmute`,
`session.instructions.append`, `session.thinking.append`,
`session.commentary.append` and `session.close`. Any other event is answered
with an `error` event and dropped; the session continues. Binary or malformed
frames, duplicate fields and frames over 256 KiB end the session.

Only client delegation is accepted. With Responses delegation Azure would call
the background model itself, outside the proxy's keys and logs. In client
delegation the client receives `session.delegation.created`, runs the task as an
ordinary request through the proxy, for example `/v1/responses` with any model
and its tools, and returns the result:

```json
{"type": "session.commentary.append", "delegation_id": "<delegation.id>", "content": "It is about 8 pm in Tokyo."}
```

`session.thinking.append` with the same ID adds quiet progress context. The
delegation event carries no text. The task follows from the transcript events
since the previous delegation.

Sessions end after `GPT_LIVE_MAX_SESSION_SECONDS`, 1800 by default. When the
client disconnects or the limit is reached, the proxy sends `session.close` so
Azure reports the final usage. Budgets are checked when a session starts, not
during it.

Each session produces one log entry with call type `_arealtime`. Its cost is the
session seconds Azure reports in `session.closed`, multiplied by the model's
price per second. Silence counts, because Azure bills the session duration. If
the connection breaks before `session.closed`, the time since `session.started`
is billed when it exceeds the last reported value.
`metadata.spend_logs_metadata.gpt_live` records the seconds, their source, the
deployment, the number of client delegations and why the session ended. Audio
and transcripts are not logged. A session that Azure refuses is logged as a
failure without cost.

### Connect over WebRTC

With WebRTC the audio flows directly between the device and Azure; the proxy
handles the setup and the control channel. Send the session and the SDP offer
to the same path:

```http
POST /v1/live/sessions
Authorization: Bearer <virtual key>

{"session": {"model": "gpt-live-1", "delegation": {"type": "client"}},
 "transport": {"type": "webrtc", "sdp": "v=0..."}}
```

The proxy runs the same checks as for the WebSocket and forwards the offer to
the deployment. An `OpenAI-Safety-Identifier` header is passed on. The answer
is `201` with `{"session": {"id": "<id>"}, "transport": {"type": "webrtc", "sdp": "<answer>"}}`.
Azure's own refusals, such as an offer without audio, keep their status and
message.

Right after the setup the proxy opens its own control connection to the
session. It records usage, enforces `GPT_LIVE_MAX_SESSION_SECONDS` and bills
the session even if the client never attaches. To receive transcripts and
delegations, connect to `wss://<proxy>/v1/live/sessions/<id>/attach` with the
same key. Events that arrived before the attach are replayed, up to 200. A
different key gets 403; an unknown or ended session gets 404. Only one client
can attach at a time. The attach accepts the same client events as the
WebSocket, including `delegation_id: null`. Unsupported events are dropped and
logged without an `error` event, because clients may end the conversation on
any error.

Billing starts with the setup. If Azure closes the control connection without
`session.closed`, for example after the device hung up, the time since the
setup is billed.

WebRTC sessions live in the memory of the worker that created them. With more
than one proxy worker, route the attach to the same worker or use a single
worker.

GPT-Live through OpenAI's own API and Responses delegation are not supported.

## Translate speech with Azure Realtime Translate

Azure Realtime Translate turns continuous speech into translated audio and
text. Clients connect with a virtual key or an OIDC API token:

```text
wss://<proxy>/v1/realtime/translations?model=<public model>
Authorization: Bearer <virtual key>
```

`model` must be the only query parameter. Add the model like any Azure model:
`azure/<deployment>` with the resource's HTTPS origin as API base, a credential
and `realtime_protocol: GA`. The deployment must run `gpt-realtime-translate`.
If its name differs, set `base_model: azure/gpt-realtime-translate` in the model
info. The price comes from `input_cost_per_second` of the deployment or from
LiteLLM's price list, otherwise from Microsoft's list price of USD 2.04 per
audio hour. Turn off the background health check for the model.

The proxy checks the key's model access, budget, limits and pre-call hooks
before it connects, and the router picks the deployment. Then the client sets
the output language and appends base64 PCM16 mono audio at 24 kHz:

```json
{"type": "session.update", "session": {"audio": {"output": {"language": "en"}}}}
{"type": "session.input_audio_buffer.append", "audio": "<base64 pcm16>"}
```

Azure streams `session.output_audio.delta` and
`session.output_transcript.delta` while audio keeps coming; there is no
`response.create`. A `session.update` may also set
`audio.input.noise_reduction` to `{"type": "near_field"}`, `{"type": "far_field"}`
or `null`, and `audio.input.transcription` to `{"model": "gpt-live-transcribe"}`
or `null`. The transcription model is an Azure model name, not a proxy alias,
and needs no model entry or key permission of its own. Source text then arrives
as `session.input_transcript.delta` on the same socket.

Any other event, other session settings, duplicate fields, binary frames and
frames over 256 KiB end the session with an `error` event and close code 1008.
An `error` from Azure ends the session with code 1011; its details are not
forwarded, because they can echo request data. Sessions end after
`LITELLM_TRANSLATE_MAX_SECONDS`, 600 by default, at most 3600.

Each session produces one log entry with call type `_arealtime`. Azure reports
no usage for translations, so the proxy counts the audio bytes it forwarded:
the cost is the input audio seconds multiplied by the price per second. Sent
silence counts; idle time and output do not.
`metadata.spend_logs_metadata.realtime_translation` records the input and output
seconds, the deployment, the Azure session ID and why the session ended. Audio
and transcripts are not logged. A session that Azure refuses is logged as a
failure without cost.

Empty sessions are not logged: Azure accepted them, no audio went either way
and the client closed normally. Clients open these to check that the service
is reachable. Set `LITELLM_TRANSLATE_LOG_EMPTY_SESSIONS=true` to log them with
zero cost. Sessions without audio that end any other way, for example with a
rejected event or a broken connection, are always logged.

Translation models are refused on `/v1/realtime` and on the realtime HTTP
endpoints, because those would reach Azure without the duration limit and the
audio count.

## Provider behavior

- ChatGPT accepts the standard client request shape and handles an upstream SSE
  response even when the upstream omits its content type.
  Chat Completions, including streaming Playground requests, use the subscription's
  Responses backend even for models available only in its live catalog.
- Parallel streamed tool calls keep their arguments attached to the correct call.
- OpenRouter speech requests can use the existing provider speech configuration.
- An MCP session belonging to another owner is reported as unavailable. Existing
  owner binding and authorization checks remain in force.
- The optional OpenRouter Jev classifier is available as a custom complexity-router
  plugin. It is not enabled by default and requires explicit provider credentials
  and routing configuration.
  Its plugin path is
  `litellm.router_strategy.complexity_router.openrouter_jev.jev_classifier`.
  When enabled, it sends bounded user-message text through OpenRouter for
  classification. The destination model may differ from the model that answers
  the request; system/assistant messages and images are not included.

## Verify an image

```sh
python3 scripts/test_extensions.py litellm-noenterprise:1.106.6
python3 scripts/test_openrouter_images.py litellm-noenterprise:1.106.6
python3 scripts/test_identity.py litellm-noenterprise:1.106.6
```

The OpenRouter browser check requires the pinned Playwright dependencies in
`upstream/litellm/tests/e2e/ui` and exercises the public proxy API, named credential resolution, and
PNG/JPEG/SVG rendering with a controlled provider fixture.

Extension checks run installed code with synthetic accounts and local mock
services. They require no real subscription, deployment credentials or external
provider requests. They do not establish live provider compatibility.

The log UI uses the generated filter and attempt types in
`subscription-logs-schema.d.ts`. Regenerate them with
`npm run gen:api -- --subscription-logs` in the dashboard directory and a Python
environment containing the current backend. The scoped export reads the actual
`/spend/logs/ui` route and the backend `SubscriptionAttributionMetadata` type.
The native route still describes Prisma metadata as generic JSON; the separate
attribution component documents its reserved fields without changing legacy
SpendLogs serialization. The UI defensively reads older or incomplete metadata
and retains its existing pagination response handling.

Bulk review bindings are generated from the dedicated admin routes with
`npm run gen:api -- --subscription-bulk`. The review version is checked atomically
by the existing model update service; ordinary model validation, encryption,
audit logging and router refresh still apply.
