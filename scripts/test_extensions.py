#!/usr/bin/env python3
"""Exercise independent extensions against an installed image with no network."""

import argparse
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    command = [
        "docker", "run", "--rm", "--network", "none",
        "--mount", f"type=bind,src={root / 'tests'},dst=/tests,readonly",
        "--entrypoint", "python", args.image,
    ]
    for suite in ("deployment_patches", "subscriptions"):
        subprocess.run(
            [*command, "-m", "unittest", "discover", "-s", f"/tests/{suite}", "-v"],
            check=True,
        )
    for scenario in ("chatgpt_compat_e2e.py", "chatgpt_routing_e2e.py"):
        subprocess.run([*command, f"/tests/deployment_patches/{scenario}"], check=True)
    subprocess.run([*command, "/tests/subscriptions/auth_e2e.py"], check=True)


if __name__ == "__main__":
    main()
