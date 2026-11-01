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

Map each ChatGPT model deployment to an explicit account alias:

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
Request logs identify the selected subscription. Catalog visibility does not
automatically create a model deployment.

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

## Provider behavior

- ChatGPT accepts the standard client request shape and handles an upstream SSE
  response even when the upstream omits its content type.
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
python3 scripts/test_identity.py litellm-noenterprise:1.106.6
```

Extension checks run installed code with synthetic accounts and local mock
services. They require no real subscription, deployment credentials or external
provider requests. They do not establish live provider compatibility.
