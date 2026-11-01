#!/usr/bin/env python3
"""Verify the installed image API and Playground against an isolated provider fixture."""
import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--source-overlay", type=Path, help="Development only: repository containing the OpenRouter adapter to mount")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    prefix = "litellm-ne-images-" + uuid.uuid4().hex[:8]
    containers = []

    def docker(*parts, check=True):
        return subprocess.run(["docker", *parts], check=check, text=True, capture_output=True)

    def run(name, *parts):
        full = prefix + "-" + name
        containers.append(full)
        return docker("run", "-d", "--name", full, "--network", prefix, "--network-alias", name, *parts)

    def wait_for(check, description, seconds=150):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError(description + " did not become ready")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text("""model_list:
  - model_name: fixture-openrouter-image
    litellm_params:
      model: openrouter/openai/gpt-image-2.5-sunburst
      api_base: http://provider:8080/api/v1
      litellm_credential_name: fixture-openrouter
    model_info:
      mode: image_generation
credential_list:
  - credential_name: fixture-openrouter
    credential_info: {}
    credential_values:
      api_key: fixture-image-provider-sentinel
general_settings:
  master_key: sk-image-fixture-only
litellm_settings:
  num_retries: 0
""")
        database = "postgresql://fixture:fixture-only@db:5432/litellm"
        try:
            docker("network", "create", prefix)
            run("db", "-e", "POSTGRES_USER=fixture", "-e", "POSTGRES_PASSWORD=fixture-only", "-e", "POSTGRES_DB=litellm", "postgres:17-alpine")
            wait_for(lambda: docker("exec", prefix + "-db", "pg_isready", "-U", "fixture", check=False).returncode == 0, "database", 60)
            docker("run", "--rm", "--network", prefix, "-e", "DATABASE_URL=" + database, "--entrypoint", "prisma", args.image, "db", "push", "--schema=/app/schema.prisma", "--skip-generate")
            run("provider", "--mount", f"type=bind,src={root / 'tests/openrouter_images/provider.py'},dst=/fixture.py,readonly", "--entrypoint", "python", args.image, "/fixture.py")
            overlay = []
            if args.source_overlay:
                source = args.source_overlay.resolve() / "upstream/litellm/litellm"
                for relative in ("llms/openrouter", "utils.py", "images/main.py"):
                    overlay.extend(["--mount", f"type=bind,src={source / relative},dst=/opt/venv/lib/python3.12/site-packages/litellm/{relative},readonly"])
            run("proxy", "-p", "127.0.0.1::4000", *overlay,
                "--mount", f"type=bind,src={config},dst=/config.yaml,readonly",
                "-e", "DATABASE_URL=" + database,
                "-e", "UI_USERNAME=admin", "-e", "UI_PASSWORD=fixture-dashboard-only",
                "-e", "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY=true",
                args.image, "--config", "/config.yaml", "--host", "0.0.0.0", "--port", "4000")
            port = docker("inspect", prefix + "-proxy", "--format", '{{(index (index .NetworkSettings.Ports "4000/tcp") 0).HostPort}}').stdout.strip()
            base = "http://127.0.0.1:" + port
            def healthy():
                if docker("inspect", prefix + "-proxy", "--format", "{{.State.Status}}").stdout.strip() == "exited":
                    raise RuntimeError("proxy exited during startup")
                with opener.open(base + "/health/liveliness", timeout=2) as response:
                    return response.status == 200
            wait_for(healthy, "proxy")
            ui = root / "upstream/litellm/tests/e2e/ui"
            subprocess.run([str(ui / "node_modules/.bin/playwright"), "test", "--config", "playwright.images.config.ts"], cwd=ui,
                           env={**os.environ, "IMAGE_PROXY_URL": base}, check=True)
            observed = docker("exec", prefix + "-proxy", "python", "-c", "import urllib.request; print(urllib.request.urlopen('http://provider:8080/control').read().decode())")
            seen = json.loads(observed.stdout)["seen"]
            assert len(seen) == 4, "Expected one API and three browser generations"
            assert seen[0]["body"] == {"model": "openai/gpt-image-2.5-sunburst", "prompt": "Synthetic API image", "size": "1024x1024", "quality": "high", "n": 1}, seen[0]
            for record, format in zip(seen[1:], ("png", "jpeg", "svg")):
                assert record["body"] == {"model": "openai/gpt-image-2.5-sunburst", "prompt": "Synthetic browser image " + format}, record
            logs = docker("logs", prefix + "-proxy")
            assert not any(marker in logs.stdout + logs.stderr for marker in ("fixture-image-provider-sentinel", "iVBORw0KGgoAAAANSUhEUg", "/9j/4AAQSkZJRg", "PHN2ZyB4bWxucz0")), "Credential or image data appeared in proxy logs"
            print("PASS: named credential, native image request/response/cost, decoded Playground image, no credential/image diagnostic leakage")
        finally:
            for name in reversed(containers):
                docker("rm", "-f", "-v", name, check=False)
            docker("network", "rm", prefix, check=False)


if __name__ == "__main__":
    main()
