# litellm-noenterprise

An independent open-source LiteLLM distribution with reproducible source imports
and a container build that excludes LiteLLM enterprise code. The initial baseline
is **LiteLLM 1.106.6**.

## Status

The reviewed upstream snapshot and independent build tooling are implemented.
The dashboard is rebuilt from source. [Generic OIDC identity](docs/identity.md)
now supports browser sessions and provider-token API access with claim-based
admission, stable accounts, and ordered role mapping. Discovery metadata and
API-token scopes share one configuration. Personal MCP connections reuse the
existing managed OAuth and client-supplied modes with per-server claim rules,
protected persistent credentials, refresh and disconnect. The existing
`/ui/connect/` page supports personal connection management without administration
access, including mobile controls and reconnect guidance. See
[personal connection usage and browser checks](docs/identity.md#manage-personal-connections).

## Build and verify

```sh
docker build -t litellm-noenterprise:1.106.6 .
python3 -m unittest discover -s tests/baseline -v
python3 scripts/audit_source.py
python3 scripts/audit_image.py litellm-noenterprise:1.106.6
python3 scripts/smoke_image.py litellm-noenterprise:1.106.6
python3 scripts/test_identity.py litellm-noenterprise:1.106.6
```

The smoke check starts isolated local containers, sends a proxy request to a
controlled upstream service, and checks the rebuilt personal connection page.
It requires no live provider credentials.

See [source provenance, build limitations, and updates](docs/upstream.md) and
[existing MCP capabilities](docs/upstream-capabilities.md).

## Work

Track work in [GitHub Issues](https://github.com/skiweg1985/litellm-noenterprise/issues).
Changes reach protected `main` through pull requests; branch conventions are in
[AGENTS.md](AGENTS.md#git-workflow). Preserve vendor ancestry with merge commits.

Next: validate the configured Entra tenant and intended MCP clients with authorized access. Controlled-service tests do not establish live interoperability.
