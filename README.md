# litellm-noenterprise

An independent open-source LiteLLM distribution with reproducible source imports
and a container build that excludes LiteLLM enterprise code. The initial baseline
is **LiteLLM 1.106.6**.

## Status

The reviewed upstream snapshot and independent build tooling are implemented.
The dashboard is rebuilt from source. Generic identity rules, provider-token API
access, and personal MCP integration are tracked in
[the implementation spec](https://github.com/skiweg1985/litellm-noenterprise/issues/2).
Existing eligible upstream functions should be reused before adding new ones.

## Build and verify

```sh
docker build -t litellm-noenterprise:1.106.6 .
python3 -m unittest discover -s tests/baseline -v
python3 scripts/audit_source.py
python3 scripts/audit_image.py litellm-noenterprise:1.106.6
python3 scripts/smoke_image.py litellm-noenterprise:1.106.6
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

Next: integrate configurable OIDC identity and API access, then extend the
existing personal MCP workflow to the agreed permission and connection models.
