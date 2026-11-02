/**
 * Regenerates src/lib/http/schema.d.ts from the proxy's OpenAPI spec.
 *
 * Two hops, because the backend is the source of truth: the FastAPI app emits
 * the spec from its route decorators (app.openapi()), then openapi-typescript
 * turns that spec into TypeScript types. There is no live server in the loop —
 * the spec is read straight off the app object, so this runs in CI without a
 * database or proxy boot.
 *
 * The Python interpreter must have litellm installed. Override which one via
 * LITELLM_PYTHON (CI passes "uv run --no-sync python"); defaults to python3.
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const dashboardDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(dashboardDir, "..", "..");
// A scoped model export avoids changing unrelated upstream API bindings when
// only the personal connection contract is consumed by this distribution.
const oidcLinks = process.argv.includes("--oidc-links");
const oidcSettings = process.argv.includes("--oidc-settings");
const personalMcp = process.argv.includes("--personal-mcp");
const subscriptions = process.argv.includes("--subscriptions");
const subscriptionBulk = process.argv.includes("--subscription-bulk");
const subscriptionUsage = process.argv.includes("--subscription-usage");
const subscriptionLogs = process.argv.includes("--subscription-logs");
let outputFile = "schema.d.ts";
if (personalMcp) outputFile = "personal-mcp-schema.d.ts";
if (subscriptions) outputFile = "subscriptions-schema.d.ts";
if (subscriptionLogs) outputFile = "subscription-logs-schema.d.ts";
if (subscriptionBulk) outputFile = "subscription-bulk-schema.d.ts";
if (subscriptionUsage) outputFile = "subscription-usage-schema.d.ts";
if (oidcLinks) outputFile = "oidc-links-schema.d.ts";
if (oidcSettings) outputFile = "oidc-settings-schema.d.ts";
const outPath = join(dashboardDir, "src", "lib", "http", outputFile);
const specDir = mkdtempSync(join(tmpdir(), "litellm-openapi-"));
const specPath = join(specDir, "openapi.json");

const python = (process.env.LITELLM_PYTHON ?? "python3").split(" ");
// The dashboard calls internal UI routes that the public /openapi.json hides via
// include_in_schema=False. Force them in so they get typed here; this mutates a
// throwaway interpreter, so the spec the proxy actually serves is unchanged.
// Python 3.13 strips a docstring's common leading indentation at compile time
// while 3.12 keeps it, so the same model yields differently-indented descriptions
// depending on the interpreter — enough to make this output non-reproducible
// across CI and contributors. inspect.cleandoc normalizes every description to one
// canonical form regardless of interpreter, so the generated file is stable.
let dumpSpec;
if (oidcLinks || oidcSettings) {
  dumpSpec = [
    "import inspect, json, sys",
    oidcLinks
      ? "from litellm.proxy.auth import oidc_links as models"
      : "from litellm.proxy.auth import oidc_settings as models",
    "schemas = {}",
    oidcLinks
      ? "for name in ('LinksResponse', 'LinkView', 'UserChoice', 'ApproveRequest', 'RevisionRequest'):"
      : "for name in ('BrowserSettings', 'SettingsResponse', 'SaveSettingsRequest', 'SaveSettingsResponse', 'CheckProviderRequest', 'CheckProviderResponse', 'DisableSettingsRequest', 'SettingsError'):",
    "    schema = getattr(models, name).model_json_schema(ref_template='#/components/schemas/{model}')",
    "    schemas.update(schema.pop('$defs', {}))",
    "    schemas[name] = schema",
    "def normalize(node):",
    "    if isinstance(node, dict): return {k: inspect.cleandoc(v) if k == 'description' and isinstance(v, str) else normalize(v) for k, v in node.items()}",
    "    if isinstance(node, list): return [normalize(v) for v in node]",
    "    return node",
    "spec = {'openapi': '3.1.0', 'info': {'title': 'Independent OIDC', 'version': '1'}, 'paths': {}, 'components': {'schemas': schemas}}",
    "with open(sys.argv[1], 'w') as f: json.dump(normalize(spec), f, sort_keys=True)",
  ].join("\n");
} else if (subscriptionUsage || subscriptionBulk) {
  dumpSpec = [
    "import json, os, sys, tempfile",
    "from fastapi import FastAPI",
    "from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Accounts",
    subscriptionBulk ? "from litellm.proxy.extensions.chatgpt_subscriptions.bulk_assignment import router" : "from litellm.proxy.extensions.chatgpt_subscriptions.observed_usage import router",
    "with tempfile.TemporaryDirectory() as directory:",
    "    os.environ['CHATGPT_TOKEN_DIR'] = directory",
    "    os.environ['CHATGPT_ACCOUNTS'] = 'default'",
    "    app = FastAPI()",
    "    app.include_router(router(Accounts(), dependency=lambda: None))",
    "    with open(sys.argv[1], 'w') as f: json.dump(app.openapi(), f, sort_keys=True)",
  ].join("\n");
} else if (subscriptionLogs) {
  dumpSpec = [
      "import inspect, json, sys",
      "from fastapi import FastAPI",
      "from pydantic import TypeAdapter",
      "from litellm.proxy.spend_tracking.spend_management_endpoints import router",
      "from litellm.types.proxy.subscription_attribution import SubscriptionAttributionMetadata",
      "app = FastAPI()",
      "for route in router.routes:",
      "    if route.path == '/spend/logs/ui':",
      "        route.include_in_schema = True",
      "        app.router.routes.append(route)",
      "spec = app.openapi()",
      "metadata = TypeAdapter(SubscriptionAttributionMetadata).json_schema(ref_template='#/components/schemas/{model}')",
      "spec['components']['schemas'].update(metadata.pop('$defs', {}))",
      "spec['components']['schemas']['SubscriptionAttributionMetadata'] = metadata",
      "spec['paths'] = {'/spend/logs/ui': spec['paths']['/spend/logs/ui']}",
      "schemas = spec['components']['schemas']",
      "selected = {}",
      "def collect(node):",
      "    if isinstance(node, dict):",
      "        ref = node.get('$ref', '')",
      "        if ref.startswith('#/components/schemas/'):",
      "            name = ref.rsplit('/', 1)[-1]",
      "            if name not in selected:",
      "                selected[name] = schemas[name]",
      "                collect(schemas[name])",
      "        for value in node.values(): collect(value)",
      "    elif isinstance(node, list):",
      "        for value in node: collect(value)",
      "collect(spec['paths'])",
      "collect({'$ref': '#/components/schemas/SubscriptionAttributionMetadata'})",
      "spec['components']['schemas'] = selected",
      "def normalize(node):",
      "    if isinstance(node, dict): return {k: inspect.cleandoc(v) if k == 'description' and isinstance(v, str) else normalize(v) for k, v in node.items()}",
      "    if isinstance(node, list): return [normalize(v) for v in node]",
      "    return node",
      "with open(sys.argv[1], 'w') as f: json.dump(normalize(spec), f, sort_keys=True)",
    ].join("\n");
} else if (subscriptions) {
  dumpSpec = [
      "import json, os, sys, tempfile",
      "from fastapi import FastAPI",
      "from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Accounts",
      "from litellm.proxy.extensions.chatgpt_subscriptions.admin import router",
      "from litellm.proxy.extensions.chatgpt_subscriptions.model_assignment import SubscriptionAssignment",
      "with tempfile.TemporaryDirectory() as directory:",
      "    os.environ['CHATGPT_TOKEN_DIR'] = directory",
      "    os.environ['CHATGPT_ACCOUNTS'] = 'default'",
      "    app = FastAPI()",
      "    for part in router(Accounts(), {}, dependency=lambda: None): app.include_router(part)",
      "    spec = app.openapi()",
      "    spec['components']['schemas']['SubscriptionAssignment'] = SubscriptionAssignment.model_json_schema()",
      "    with open(sys.argv[1], 'w') as f: json.dump(spec, f, sort_keys=True)",
    ].join("\n");
} else if (personalMcp) {
  dumpSpec = [
      "import json, sys",
      "from litellm.proxy._types import MCPOAuthUserCredentialStatus",
      "spec = {'openapi': '3.1.0', 'info': {'title': 'Personal MCP connection status', 'version': '1'}, 'paths': {}, 'components': {'schemas': {'MCPOAuthUserCredentialStatus': MCPOAuthUserCredentialStatus.model_json_schema()}}}",
      "with open(sys.argv[1], 'w') as f: json.dump(spec, f, sort_keys=True)",
    ].join("\n");
} else {
  dumpSpec = [
      "import inspect, json, sys",
      "from litellm.proxy.proxy_server import app",
      "from fastapi.routing import APIRoute",
      "for route in app.routes:",
      "    if isinstance(route, APIRoute):",
      "        route.include_in_schema = True",
      "app.openapi_schema = None",
      "def normalize(node):",
      "    if isinstance(node, dict):",
      "        return {k: inspect.cleandoc(v) if k == 'description' and isinstance(v, str) else normalize(v) for k, v in node.items()}",
      "    if isinstance(node, list):",
      "        return [normalize(v) for v in node]",
      "    return node",
      "with open(sys.argv[1], 'w') as f: json.dump(normalize(app.openapi()), f, sort_keys=True)",
    ].join("\n");
}

try {
  execFileSync(python[0], [...python.slice(1), "-c", dumpSpec, specPath], {
    cwd: repoRoot,
    stdio: "inherit",
  });

  execFileSync(join(dashboardDir, "node_modules", ".bin", "openapi-typescript"), [specPath, "-o", outPath, ...(oidcSettings ? ["--default-non-nullable", "false"] : [])], {
    cwd: dashboardDir,
    stdio: "inherit",
  });
} finally {
  rmSync(specDir, { recursive: true, force: true });
}
