"""Synthetic HTTP fixture helpers. No production captures or credentials."""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

FIXTURES = json.loads((Path(__file__).parent / "fixtures/chatgpt_sse.json").read_text())


def jwt(claims):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.synthetic-signature"


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'} {name}{'' if condition else ': ' + str(detail)[:300]}", flush=True)
    return bool(condition)


def request(method, path, key=None, body=None, headers=None, port=14020):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={**({"Authorization": f"Bearer {key}"} if key else {}),
                 **({"Content-Type": "application/json"} if data else {}), **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            text = response.read().decode()
            return response.status, (json.loads(text) if text.startswith(("{", "[")) else text)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]
    except (urllib.error.URLError, ConnectionError) as exc:
        return 0, str(exc)


def wait_for(fetch, predicate, timeout=40):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = fetch()
        if predicate(value):
            return True, value
        time.sleep(0.5)
    return False, value
