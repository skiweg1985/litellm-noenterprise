#!/usr/bin/env python3
"""Run public OIDC acceptance tests using isolated Docker fixtures."""

import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("image", default="litellm-noenterprise:baseline", nargs="?")
parser.add_argument("--browser", action="store_true", help="Run the personal UI browser journeys on loopback ports 4000, 8080, 8090")
parser.add_argument("--browser-admin-disabled", action="store_true")
parser.add_argument("--reject-missing-master", action="store_true")
parser.add_argument("--reject-invalid-api", action="store_true")
parser.add_argument("--test-pattern", default="test*.py")
parser.add_argument("--proxy-workers", type=int, choices=range(1, 9), default=1, help="Run independent proxy workers against the same synthetic database and registry")
parser.add_argument("--response-cache", action="store_true", help="Enable local response caching for cache attribution checks")
parser.add_argument("--api-scope-name", default="api://litellm-api/access")
parser.add_argument("--api-token-scope", default="access")
parser.add_argument("--api-audience", default="litellm-api")
parser.add_argument("--master-key", default="sk-identity-test-only")
parser.add_argument("--api-token-type", choices=["JWT", "at+jwt"], default="JWT")
parser.add_argument(
    "--source-overlay",
    action="store_true",
    help="Development only: run mounted source instead of installed image code",
)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
prefix = "litellm-ne-identity-" + uuid.uuid4().hex[:8]
containers = []


def docker(*parts, check=True):
    return subprocess.run(
        ["docker", *parts], check=check, text=True, capture_output=True
    )


def run(name, *parts):
    full = prefix + "-" + name
    containers.append(full)
    return docker(
        "run",
        "-d",
        "--name",
        full,
        "--network",
        prefix,
        "--network-alias",
        name,
        *parts,
    )


with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
    config = Path(tmp) / "config.yaml"
    config.write_text("""model_list:
  - model_name: fixture-model
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: fixture-unused
      mock_response: fixture completion
mcp_servers:
  fixture:
    url: http://mcp-upstream:8090/mcp
    transport: http
  delegate:
    url: http://mcp-upstream:8090/mcp
    transport: http
    auth_type: oauth_delegate
  personal:
    url: http://mcp-upstream:8090/mcp
    transport: http
    auth_type: oauth2
    oauth2_flow: authorization_code
    authorization_url: http://issuer:8080/authorize
    token_url: http://issuer:8080/token
    client_id: litellm-test
  managed_a:
    server_id: managed_a
    url: http://mcp-upstream:8090/mcp
    transport: http
    auth_type: oauth2
    oauth2_flow: authorization_code
    authorization_url: http://mcp-upstream:8090/oauth/authorize
    token_url: http://mcp-upstream:8090/oauth/token
    client_id: mcp-test
  managed_b:
    server_id: managed_b
    url: http://mcp-upstream:8090/mcp
    transport: http
    auth_type: oauth2
    oauth2_flow: authorization_code
    authorization_url: http://mcp-upstream:8090/oauth/authorize
    token_url: http://mcp-upstream:8090/oauth/token
    client_id: mcp-test
  managed_unconfigured:
    server_id: managed_unconfigured
    url: http://mcp-upstream:8090/mcp
    transport: http
    auth_type: oauth2
    oauth2_flow: authorization_code
    authorization_url: http://mcp-upstream:8090/oauth/authorize
    token_url: http://mcp-upstream:8090/oauth/token
general_settings:
  store_model_in_db: true
  master_key: sk-identity-test-only
  oidc_api:
    resource: http://proxy:4000
    audience: litellm-api
    scope_claim: scp
    token_types: [JWT]
    required_claims:
      - {claim: identity_kind, any_of: [user]}
    scopes:
      - {name: 'api://litellm-api/access', token_value: access}
  oidc_identity:
    issuer: http://issuer:8080
    client_id: litellm-test
    client_secret_env: OIDC_TEST_CLIENT_SECRET
    redirect_uri: http://proxy:4000/oidc/callback
    allow_insecure_http: true
    subject_claim: oid
    mcp_access_rules:
      managed_a: {claim: roles, any_of: [mcp-a]}
      managed_b: {claim: roles, any_of: [mcp-b]}
      managed_unconfigured: {claim: roles, any_of: [mcp-setup]}
    access_rule:
      claim: roles
      any_of: [access]
    role_mappings:
      - rule: {claim: roles, any_of: [admin]}
        role: proxy_admin
      - rule: {claim: roles, any_of: [operator]}
        role: internal_user
    default_role: internal_user_viewer
""")
    value = config.read_text().replace("api://litellm-api/access", args.api_scope_name)
    if args.browser:
        value = value.replace("    mcp_access_rules:\n", "    mcp_access_rules:\n      delegate: {claim: roles, any_of: [mcp-delegate]}\n")
    value = value.replace(
        "token_value: access", "token_value: " + json.dumps(args.api_token_scope)
    )
    value = value.replace(
        "audience: litellm-api", "audience: " + json.dumps(args.api_audience)
    )
    value = value.replace(
        "token_types: [JWT]", "token_types: " + json.dumps([args.api_token_type])
    )
    if not args.reject_missing_master:
        value = value.replace(
            "master_key: sk-identity-test-only",
            "master_key: " + json.dumps(args.master_key),
        )
    if args.response_cache:
        value += "\nlitellm_settings:\n  cache: true\n  cache_params:\n    type: local\n"
    if args.test_pattern == "test_subscription_headers.py":
        value = value.replace("general_settings:\n", "general_settings:\n  forward_client_headers_to_llm_api: true\n", 1)
    config.write_text(value)
    if args.reject_missing_master:
        config.write_text(
            config.read_text().replace("  master_key: sk-identity-test-only\n", "")
        )
    database = "postgresql://fixture:fixture-only@db:5432/litellm"
    try:
        docker("network", "create", prefix)
        run(
            "db",
            "-e",
            "POSTGRES_USER=fixture",
            "-e",
            "POSTGRES_PASSWORD=fixture-only",
            "-e",
            "POSTGRES_DB=litellm",
            "postgres:17-alpine",
        )
        for _ in range(60):
            if (
                docker(
                    "exec", prefix + "-db", "pg_isready", "-U", "fixture", check=False
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError("database did not become ready")
        docker(
            "run",
            "--rm",
            "--network",
            prefix,
            "-e",
            "DATABASE_URL=" + database,
            "--entrypoint",
            "prisma",
            args.image,
            "db",
            "push",
            "--schema=/app/schema.prisma",
            "--skip-generate",
        )
        mount = ["--mount", f"type=bind,src={root},dst=/work,readonly"]
        run(
            "mcp-upstream",
            *(["-p", "127.0.0.1:8090:8090"] if args.browser else []),
            *mount,
            "--entrypoint",
            "python",
            args.image,
            "/work/tests/identity/fixtures/mcp_server.py",
        )
        run(
            "chatgpt",
            *(["-p", "127.0.0.1:8091:8091"] if args.browser else []),
            *mount,
            "--entrypoint", "python", args.image,
            "/work/tests/identity/fixtures/chatgpt.py",
        )
        run(
            "issuer",
            *(["-p", "127.0.0.1:8080:8080"] if args.browser else []),
            *mount,
            "--entrypoint",
            "python",
            args.image,
            "/work/tests/identity/fixtures/issuer.py",
        )
        source_env = (
            ["-e", "PYTHONPATH=/work/upstream/litellm"] if args.source_overlay else []
        )
        run(
            "proxy",
            *(["-p", "127.0.0.1:4000:4000"] if args.browser else []),
            *mount,
            *source_env,
            *(["-e", "DISABLE_ADMIN_UI=true"] if args.browser_admin_disabled else []),
            "--mount",
            f"type=bind,src={config},dst=/test/config.yaml,readonly",
            "-e",
            "DATABASE_URL=" + database,
            "-e",
            "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY=true",
            "-e",
            "OIDC_TEST_CLIENT_SECRET=fixture-only",
            "-e", "CHATGPT_AUTH_BASE=http://chatgpt:8091",
            "-e", "CHATGPT_API_BASE=http://chatgpt:8091",
            "-e", "CHATGPT_MODELS_URL=http://chatgpt:8091/models",
            "-e", "CHATGPT_USAGE_URL=http://chatgpt:8091/usage",
            "-e", "CHATGPT_DEVICE_POLL_MIN_SECONDS=1",
            "-e",
            "LITELLM_OIDC_SESSION_SECRET=fixture-session-secret-32-characters-minimum",
            args.image,
            "--config",
            "/test/config.yaml",
            "--host",
            "0.0.0.0",
            "--port",
            "4000",
            "--use_prisma_db_push",
            "--num_workers",
            str(args.proxy_workers),
        )
        for _ in range(120):
            result = docker(
                "exec",
                prefix + "-proxy",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness')",
                check=False,
            )
            if result.returncode == 0:
                if args.reject_missing_master or args.reject_invalid_api:
                    raise AssertionError("Invalid OIDC configuration still started")
                break
            if (args.reject_missing_master or args.reject_invalid_api) and docker(
                "inspect", prefix + "-proxy", "--format", "{{.State.Status}}"
            ).stdout.strip() == "exited":
                logs = docker("logs", prefix + "-proxy", check=False)
                expected = (
                    "OIDC requires a master key"
                    if args.reject_missing_master
                    else "OIDC API configuration is invalid"
                )
                if expected not in logs.stdout + logs.stderr:
                    raise AssertionError("Unexpected startup failure")
                print("PASS: " + expected + " (startup rejected)")
                raise SystemExit(0)
            if (
                docker(
                    "inspect", prefix + "-proxy", "--format", "{{.State.Status}}"
                ).stdout.strip()
                == "exited"
            ):
                raise RuntimeError("proxy exited during startup")
            time.sleep(1)
        else:
            raise RuntimeError("proxy did not become ready")
        if args.browser:
            ui = root / "upstream/litellm/tests/e2e/ui"
            subprocess.run(
                [str(ui / "node_modules/.bin/playwright"), "test", "--config", "playwright.personal.config.ts"],
                cwd=ui,
                env={**os.environ, "UI_ADMIN_DISABLED": str(args.browser_admin_disabled).lower()},
                check=True,
            )
            raise SystemExit(0)
        result = docker(
            "run",
            "--rm",
            "--network",
            prefix,
            *mount,
            "-e",
            "EXPECTED_API_SCOPE=" + args.api_scope_name,
            "-e",
            "EXPECTED_TOKEN_SCOPE=" + args.api_token_scope,
            "-e",
            "EXPECTED_API_AUDIENCE=" + args.api_audience,
            "-e",
            "EXPECTED_TOKEN_TYPE=" + args.api_token_type,
            "-e",
            "EXPECTED_MASTER_KEY=" + args.master_key,
            "-e",
            "RESPONSE_CACHE_ENABLED=" + str(args.response_cache).lower(),
            "--entrypoint",
            "python",
            args.image,
            "-m",
            "unittest",
            "discover",
            "-s",
            "/work/tests/identity",
            "-v",
            "-p",
            args.test_pattern,
            check=False,
        )
        print(result.stdout)
        print(result.stderr)
        if result.returncode:
            raise RuntimeError("identity acceptance tests failed")
        if args.test_pattern in ("test*.py", "test_mcp.py", "test_subscription_models.py"):
            docker("restart", prefix + "-proxy")
            for _ in range(90):
                ready = docker(
                    "exec",
                    prefix + "-proxy",
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness')",
                    check=False,
                )
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("proxy did not restart")
            restart_tests = []
            if args.test_pattern in ("test*.py", "test_mcp.py"):
                restart_tests.append("test_mcp.PersonalMCPTests.test_connection_survives_cold_restart")
            if args.test_pattern in ("test*.py", "test_subscription_models.py"):
                restart_tests.append("test_subscription_models.SubscriptionModelTests.test_assignment_survives_restart")
            for test in restart_tests:
                persistence = docker(
                    "run", "--rm", "--network", prefix, *mount,
                    "-e", "MCP_RESTART_PHASE=read",
                    "-e", "SUBSCRIPTION_RESTART_PHASE=read",
                    "--workdir", "/work/tests/identity",
                    "--entrypoint", "python", args.image,
                    "-m", "unittest", "-v", test, check=False,
                )
                print(persistence.stdout)
                print(persistence.stderr)
                if persistence.returncode:
                    raise RuntimeError("cold-restart persistence failed: " + test)
        logs = docker("logs", prefix + "-proxy", check=False)
        if any(
            marker in logs.stdout + logs.stderr
            for marker in (
                "mcp-fixture-access-",
                "mcp-fixture-refresh-",
                "client-supplied-sentinel",
            )
        ):
            raise AssertionError("MCP credential material appeared in proxy logs")
    except Exception:
        for name in containers:
            if name.endswith(("-proxy", "-issuer")):
                logs = docker("logs", name, check=False)
                print(logs.stdout[-10000:])
                print(logs.stderr[-10000:])
        raise
    finally:
        for name in reversed(containers):
            docker("rm", "-f", "-v", name, check=False)
        docker("network", "rm", prefix, check=False)
