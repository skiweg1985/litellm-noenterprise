# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
ARG NODE_IMAGE=node:24.22.2-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a
FROM ${UV_IMAGE} AS uv
FROM ${NODE_IMAGE} AS ui
WORKDIR /ui
ENV NEXT_TELEMETRY_DISABLED=1 NODE_OPTIONS=--max-old-space-size=6144
COPY upstream/litellm/ui/litellm-dashboard/package*.json upstream/litellm/ui/litellm-dashboard/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY upstream/litellm/ui/litellm-dashboard/ ./
RUN npm run build
COPY scripts/collect_license_notices.mjs /build-tools/collect_license_notices.mjs
COPY runtime/licenses/ /build-tools/licenses/
RUN node /build-tools/collect_license_notices.mjs --node-modules /ui/node_modules --node-license /usr/local/LICENSE --output /ui/out/third-party-notices --supplements /build-tools/licenses/supplements.json

FROM ${PYTHON_IMAGE} AS base
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 libstdc++6 openssl libsndfile1 && rm -rf /var/lib/apt/lists/*

FROM base AS build
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=ui /usr/local/bin/node /usr/local/bin/node
COPY --from=ui /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
ENV UV_LINK_MODE=copy PRISMA_BINARY_CACHE_DIR=/opt/prisma PRISMA_USE_GLOBAL_NODE=1 PATH=/opt/venv/bin:$PATH
RUN uv venv /opt/venv
COPY runtime/requirements.lock /build/requirements.lock
RUN uv pip install --python /opt/venv/bin/python --require-hashes -r /build/requirements.lock
COPY upstream/litellm/litellm/ /build/package/litellm/
COPY upstream/litellm/LICENSE /build/package/LICENSE
COPY runtime/pyproject.toml /build/package/pyproject.toml
COPY --from=ui /ui/out/ /build/package/litellm/proxy/_experimental/out/
COPY upstream/litellm/litellm-proxy-extras/ /build/proxy-extras/
RUN uv pip install --python /opt/venv/bin/python --no-deps /build/package /build/proxy-extras
COPY upstream/litellm/schema.prisma /app/schema.prisma
RUN /opt/venv/bin/prisma generate --schema=/app/schema.prisma
RUN /opt/venv/bin/python -c "import importlib.util; assert importlib.util.find_spec('litellm_enterprise') is None; assert importlib.util.find_spec('enterprise') is None"

FROM base AS runtime
COPY --from=ui /usr/local/bin/node /usr/local/bin/node
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/prisma /opt/prisma
COPY --from=build /app/schema.prisma /app/schema.prisma
COPY upstream/litellm/LICENSE /usr/share/licenses/litellm/LICENSE
COPY --from=ui /ui/out/third-party-notices/ /usr/share/licenses/litellm-noenterprise/
COPY LICENSE /usr/share/licenses/litellm-noenterprise/LICENSE
ENV PATH=/opt/venv/bin:$PATH PRISMA_BINARY_CACHE_DIR=/opt/prisma PRISMA_USE_GLOBAL_NODE=1 \
    PRISMA_CLI_PATH=/opt/prisma/node_modules/.bin/prisma PRISMA_OFFLINE_MODE=true \
    LITELLM_LOCAL_MODEL_COST_MAP=True LITELLM_TELEMETRY=False PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --uid 10001 --create-home litellm && chown -R 10001:10001 /app /opt/prisma
USER 10001
EXPOSE 4000
ENTRYPOINT ["litellm"]
CMD ["--host", "0.0.0.0", "--port", "4000"]
