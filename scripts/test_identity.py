#!/usr/bin/env python3
"""Run public OIDC acceptance tests using isolated Docker fixtures."""

import argparse
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("image", default="litellm-noenterprise:baseline", nargs="?")
parser.add_argument("--reject-missing-master", action="store_true")
parser.add_argument(
    "--source-overlay", action="store_true", help="Development only: run mounted source instead of installed image code"
)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
prefix = "litellm-ne-identity-" + uuid.uuid4().hex[:8]
containers = []


def docker(*parts, check=True):
    return subprocess.run(["docker", *parts], check=check, text=True, capture_output=True)


def run(name, *parts):
    full = prefix + "-" + name
    containers.append(full)
    return docker("run", "-d", "--name", full, "--network", prefix, "--network-alias", name, *parts)


with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
    config = Path(tmp) / "config.yaml"
    config.write_text("""mcp_servers:
  fixture:
    url: http://mcp-upstream:8090/mcp
    transport: http
general_settings:
  master_key: sk-identity-test-only
  oidc_identity:
    issuer: http://issuer:8080
    client_id: litellm-test
    client_secret_env: OIDC_TEST_CLIENT_SECRET
    redirect_uri: http://proxy:4000/oidc/callback
    allow_insecure_http: true
    subject_claim: oid
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
    if args.reject_missing_master:
        config.write_text(config.read_text().replace("  master_key: sk-identity-test-only\n", ""))
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
            if docker("exec", prefix + "-db", "pg_isready", "-U", "fixture", check=False).returncode == 0:
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
        run("mcp-upstream", *mount, "--entrypoint", "python", args.image, "/work/tests/identity/fixtures/mcp_server.py")
        run("issuer", *mount, "--entrypoint", "python", args.image, "/work/tests/identity/fixtures/issuer.py")
        source_env = ["-e", "PYTHONPATH=/work/upstream/litellm"] if args.source_overlay else []
        run(
            "proxy",
            *mount,
            *source_env,
            "--mount",
            f"type=bind,src={config},dst=/test/config.yaml,readonly",
            "-e",
            "DATABASE_URL=" + database,
            "-e",
            "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY=true",
            "-e",
            "OIDC_TEST_CLIENT_SECRET=fixture-only",
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
                if args.reject_missing_master:
                    raise AssertionError("OIDC configured without a master key still started")
                break
            if (
                args.reject_missing_master
                and docker("inspect", prefix + "-proxy", "--format", "{{.State.Status}}").stdout.strip() == "exited"
            ):
                logs = docker("logs", prefix + "-proxy", check=False)
                if "OIDC requires a master key" not in logs.stdout + logs.stderr:
                    raise AssertionError("Unexpected startup failure")
                print("PASS: OIDC without a master key is rejected at startup")
                raise SystemExit(0)
            time.sleep(1)
        else:
            raise RuntimeError("proxy did not become ready")
        result = docker(
            "run",
            "--rm",
            "--network",
            prefix,
            *mount,
            "--entrypoint",
            "python",
            args.image,
            "-m",
            "unittest",
            "discover",
            "-s",
            "/work/tests/identity",
            "-v",
            check=False,
        )
        print(result.stdout)
        print(result.stderr)
        if result.returncode:
            raise RuntimeError("identity acceptance tests failed")
    except Exception:
        for name in containers:
            if name.endswith(("-proxy", "-issuer")):
                logs = docker("logs", name, check=False)
                print(logs.stdout[-10000:])
                print(logs.stderr[-10000:])
        raise
    finally:
        for name in reversed(containers):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", prefix, check=False)
