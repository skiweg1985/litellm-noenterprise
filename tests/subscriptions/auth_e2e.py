"""End-to-end check of the ChatGPT sign-in inside the built image.

Runs without network access (see tests/subscriptions/auth_e2e.py). Local
servers stand in for OpenAI's sign-in service and the ChatGPT backend; all
tokens are fake.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AUTH_PORT, BACKEND_PORT, PROXY_PORT = 18801, 18802, 14010
KEY, MONITOR = "sk-e2e-master", "monitor-e2e"
MODEL = "gpt-6-astra-sub"
FIXTURES = json.loads((Path(__file__).parents[1] / "deployment_patches/fixtures/chatgpt_sse.json").read_text())
# No network: use the model cost map bundled with LiteLLM.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY"] = "true"


def jwt(claims):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


class Auth:
    """OpenAI's sign-in service; refresh tokens rotate and cannot be reused."""

    lock = threading.Lock()
    refresh_token = "refresh-0"
    serial = 0
    refreshes = 0
    reused = 0
    device_codes = 0
    confirmed = False
    email = "e2e@example.com"

    @classmethod
    def issue(cls):
        cls.serial += 1
        cls.refresh_token = f"refresh-{cls.serial}"
        token = jwt({"exp": int(time.time()) + 10 * 86400, "serial": cls.serial, "email": cls.email,
                     "https://api.openai.com/auth": {"chatgpt_account_id": "acct-e2e"}})
        return {"access_token": token, "id_token": token, "refresh_token": cls.refresh_token}


class AuthHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path == "/oauth/token" and self.headers.get("content-type", "").startswith("application/json"):
            time.sleep(0.3)  # widen the window for concurrent refreshes
            with Auth.lock:
                if json.loads(body)["refresh_token"] != Auth.refresh_token:
                    Auth.reused += 1
                    return self.reply(401, {"error": "refresh_token_reused"})
                Auth.refreshes += 1
                return self.reply(200, Auth.issue())
        if self.path == "/oauth/token":
            form = urllib.parse.parse_qs(body.decode())
            assert form["grant_type"] == ["authorization_code"] and form["code_verifier"] == ["verifier"]
            with Auth.lock:
                return self.reply(200, Auth.issue())
        if self.path == "/api/accounts/deviceauth/usercode":
            Auth.device_codes += 1
            return self.reply(200, {"device_auth_id": "dev-e2e", "user_code": "TEST-CODE", "interval": 1})
        if self.path == "/api/accounts/deviceauth/token":
            if not Auth.confirmed:
                return self.reply(403, {})
            return self.reply(200, {"authorization_code": "auth-code", "code_challenge": "c", "code_verifier": "verifier"})
        self.reply(404, {})

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Backend(BaseHTTPRequestHandler):
    seen = []  # (bearer token, ChatGPT-Account-Id) per model request

    def do_GET(self):  # quota and models endpoints
        if self.path.startswith("/codex/models?client_version="):
            entry = {"visibility": "list", "priority": 1, "base_instructions": "cli instructions", "context_window": 272000,
                     "supported_reasoning_levels": [{"effort": "low", "description": "low"}]}
            data = json.dumps({"models": [{**entry, "slug": "gpt-6-astra", "display_name": "GPT-6-Astra"},
                                          {**entry, "slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "priority": 0},
                                          {**entry, "slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        data = json.dumps({"rate_limit": {"allowed": True, "primary_window": {
            "used_percent": 10, "limit_window_seconds": 18000, "reset_at": int(time.time()) + 600}}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        Backend.seen.append((self.headers.get("authorization", "")[len("Bearer "):],
                             self.headers.get("chatgpt-account-id")))
        self.send_response(200)
        self.end_headers()
        for event in deepcopy(FIXTURES["text"]):
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

    def log_message(self, *args):
        pass


CHILD = r"""
import json, sys, threading, time
import litellm.llms.chatgpt.authenticator as auth
auth.CHATGPT_OAUTH_TOKEN_URL = sys.argv[1]
start = float(sys.argv[2])
results = []
def worker():
    try:
        results.append(auth.Authenticator().get_access_token())
    except Exception as exc:
        results.append("ERROR " + type(exc).__name__)
while time.time() < start:
    time.sleep(0.001)
threads = [threading.Thread(target=worker) for _ in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
print(json.dumps(results))
"""


def check(name, condition, detail=""):
    print(f"{'ok    ' if condition else 'FAILED'}  {name}{'' if condition else ': ' + str(detail)[:300]}", flush=True)
    return bool(condition)


def concurrent_refresh(directory):
    path = Path(directory, "auth.json")
    expired = jwt({"exp": int(time.time()) - 10})
    path.write_text(json.dumps({"access_token": expired, "id_token": expired, "refresh_token": "refresh-0",
                                "expires_at": time.time() - 10, "account_id": "acct-e2e"}))
    stop, broken = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                if not isinstance(json.loads(path.read_text()), dict):
                    broken.append("not an object")
            except (OSError, ValueError) as exc:
                broken.append(type(exc).__name__)

    threading.Thread(target=reader, daemon=True).start()
    env = {**os.environ, "CHATGPT_TOKEN_DIR": directory}
    start = time.time() + 6
    children = [subprocess.Popen([sys.executable, "-c", CHILD, f"http://127.0.0.1:{AUTH_PORT}/oauth/token", str(start)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) for _ in range(8)]
    results = [token for child in children for token in json.loads(child.communicate(timeout=120)[0] or "[]")]
    stop.set()
    ok = check("32 concurrent requests share one refresh", Auth.refreshes == 1 and Auth.reused == 0,
               f"refreshes={Auth.refreshes} reused={Auth.reused}")
    ok &= check("every request got the new token", len(set(results)) == 1 and not results[0].startswith("ERROR"),
                sorted(set(r[:40] for r in results)))
    ok &= check("readers never saw a partial file", not broken, broken[:5])
    ok &= check("the file holds the rotated refresh token", json.loads(path.read_text())["refresh_token"] == Auth.refresh_token)

    path.write_text(json.dumps({"access_token": expired, "expires_at": time.time() - 10}))
    began = time.time()
    code = ("import litellm.llms.chatgpt.authenticator as a\n"
            "try:\n    a.Authenticator().get_access_token()\nexcept Exception as e:\n    print(type(e).__name__)\n")
    output = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60).stdout.strip()
    ok &= check("no token: fails fast instead of a device sign-in",
                output == "GetAccessTokenError" and Auth.device_codes == 0 and time.time() - began < 30, output)
    return ok


def request(method, path, key=None, body=None, headers=None, port=PROXY_PORT):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                     headers={**({"Authorization": f"Bearer {key}"} if key else {}),
                                              **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode()
            return response.status, (json.loads(text) if text.startswith(("{", "[")) else text)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]
    except (urllib.error.URLError, ConnectionError) as exc:
        return 0, str(exc)


def wait_for(fetch, predicate, timeout=40):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = fetch()
        if predicate(value):
            return True, value
        time.sleep(0.5)
    return False, value


def proxy_flow(directory):
    Auth.confirmed = False
    config = Path(directory, "config.yaml")
    config.write_text("credential_list:\n  - credential_name: chatgpt-default\n"
                      "    credential_values:\n      api_key: chatgpt-account:default\n"
                      "    credential_info:\n      description: ChatGPT-Abo default\n"
                      f"model_list:\n  - model_name: {MODEL}\n    model_info:\n      mode: responses\n"
                      f"    litellm_params:\n      model: chatgpt/gpt-6-astra\n      order: 1\n"
                      "      litellm_credential_name: chatgpt-default\n"
                      f"general_settings:\n  master_key: {KEY}\n")
    env = {**os.environ, "CHATGPT_TOKEN_DIR": directory, "CHATGPT_AUTH_BASE": f"http://127.0.0.1:{AUTH_PORT}",
           "CHATGPT_API_BASE": f"http://127.0.0.1:{BACKEND_PORT}", "CHATGPT_USAGE_URL": f"http://127.0.0.1:{BACKEND_PORT}/wham/usage",
           "CHATGPT_MODELS_URL": f"http://127.0.0.1:{BACKEND_PORT}/codex/models",
           "CHATGPT_MONITORING_API_KEY": MONITOR,
        "CHATGPT_AUTO_LOGIN": "true", "CHATGPT_DEVICE_POLL_MIN_SECONDS": "1",
           "CHATGPT_WATCH_INTERVAL_SECONDS": "1", "CHATGPT_EXPECTED_ACCOUNT_ID": "acct-e2e"}
    for name in ("DATABASE_URL", "STORE_MODEL_IN_DB"):
        env.pop(name, None)
    log = open(Path(directory, "proxy.log"), "w")
    proxy = subprocess.Popen(["litellm", "--config", str(config), "--port", str(PROXY_PORT)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        ready, _ = wait_for(lambda: request("GET", "/health/liveliness")[0], lambda s: s == 200, 120)
        ok = check("proxy starts without a ChatGPT token", ready)
        models = request("GET", "/v1/models", KEY)[1]
        ok &= check("model stays registered without a token", MODEL in json.dumps(models), models)

        chat = {"model": MODEL, "messages": [{"role": "user", "content": "Sag nur: ok"}]}
        began = time.time()
        status, body = request("POST", "/v1/chat/completions", KEY, chat)
        ok &= check("request without sign-in fails fast with 401", status == 401 and time.time() - began < 10,
                    f"{status} after {time.time() - began:.1f}s: {body}")

        prtg = lambda: request("GET", "/chatgpt/accounts/default/usage/prtg", headers={"X-Monitoring-Key": MONITOR})[1]
        seen, sensor = wait_for(prtg, lambda r: isinstance(r, dict) and "Login required" in r["prtg"].get("text", ""))
        ok &= check("PRTG reports sign-in required without publishing the code", seen and sensor["prtg"]["error"] == 1 and "TEST-CODE" not in json.dumps(sensor), sensor)

        ok &= check("admin API rejects requests without a key", request("GET", "/chatgpt/api/accounts")[0] == 401)
        state = request("GET", "/chatgpt/api/accounts", KEY)[1]
        ok &= check("admin API reports the pending sign-in",
                    state["accounts"][0]["state"] == "login_pending" and state["accounts"][0]["login"]["user_code"] == "TEST-CODE", state)

        Auth.confirmed = True
        signed_in, state = wait_for(lambda: request("GET", "/chatgpt/api/accounts", KEY)[1],
                                    lambda s: isinstance(s, dict) and s["accounts"][0]["state"] == "ok")
        ok &= check("sign-in completes in the background", signed_in and state["accounts"][0]["email"] == "e2e@example.com", state)
        began = time.time()
        works, (status, body) = wait_for(lambda: request("POST", "/v1/chat/completions", KEY, chat), lambda r: r[0] == 200, 120)
        print(f"        first successful request {time.time() - began:.1f}s after the sign-in", flush=True)
        ok &= check("requests work after the sign-in without a restart",
                    works and body["choices"][0]["message"]["content"] == "ok", body)
        sensor = request("GET", "/chatgpt/accounts/default/usage/prtg?windows=primary", headers={"X-Monitoring-Key": MONITOR})[1]
        ok &= check("PRTG reports the quota again", "error" not in sensor["prtg"] and sensor["prtg"]["result"][1]["value"] == 90, sensor)

        before = Auth.refreshes
        status, body = request("POST", "/chatgpt/api/accounts/default/refresh", KEY)
        ok &= check("manual refresh", status == 200 and Auth.refreshes == before + 1 and body["state"] == "ok", body)
        status, body = request("POST", "/chatgpt/api/accounts/default/logout", KEY)
        ok &= check("logout", status == 200 and body["state"] == "logged_out", body)
        time.sleep(2)
        codes = Auth.device_codes
        ok &= check("no automatic sign-in after a logout", request("GET", "/chatgpt/api/accounts", KEY)[1]["accounts"][0]["state"] == "logged_out" and Auth.device_codes == codes)
        status, body = request("POST", "/v1/chat/completions", KEY, chat)
        ok &= check("requests fail again after the logout", status == 401, f"{status}: {body}")

        Auth.email = "successor@example.com"
        request("POST", "/chatgpt/api/accounts/default/login", KEY)
        taken, state = wait_for(lambda: request("GET", "/chatgpt/api/accounts", KEY)[1],
                                lambda s: isinstance(s, dict) and s["accounts"][0]["state"] == "ok")
        ok &= check("after a logout another person signs in without confirmation",
                    taken and state["accounts"][0]["known_account"]["email"] == "successor@example.com", state)

        Auth.email, Auth.confirmed = "colleague@example.com", False
        request("POST", "/chatgpt/api/accounts/default/login", KEY)
        Auth.confirmed = True
        held, state = wait_for(lambda: request("GET", "/chatgpt/api/accounts", KEY)[1],
                               lambda s: isinstance(s, dict) and s["accounts"][0]["pending_account"])
        ok &= check("a different person's sign-in is held back",
                    held and state["accounts"][0]["pending_account"]["email"] == "colleague@example.com"
                    and state["accounts"][0]["email"] == "successor@example.com", state)
        sensor = prtg()
        ok &= check("PRTG alarms without naming the person",
                    sensor["prtg"].get("error") == 1 and "different ChatGPT account" in sensor["prtg"]["text"]
                    and "colleague" not in sensor["prtg"]["text"], sensor)
        status, body = request("POST", "/chatgpt/api/accounts/default/pending/discard", KEY)
        ok &= check("discarding keeps the colleague's tokens out",
                    status == 200 and body["pending_account"] is None and body["token_valid"]
                    and body["email"] == "successor@example.com"
                    and body["known_account"]["email"] == "successor@example.com", body)
        return ok
    finally:
        proxy.terminate()
        proxy.wait(timeout=30)
        log.close()


ACCOUNT_CHILD = r"""
import json
import litellm.llms.chatgpt.authenticator as auth
result = {}
for key in (None, "chatgpt-account:team-2", "chatgpt-account:nobody", "chatgpt-account:../x", "sk-other"):
    try:
        a = auth.Authenticator.for_request({"api_key": key})
        result[str(key)] = [a.account, a.auth_file]
    except Exception as exc:
        result[str(key)] = type(exc).__name__ + ": " + str(exc)
result["same"] = auth.Authenticator.for_request(None, "chatgpt-account:team-2") is auth.Authenticator.for_request({"api_key": "chatgpt-account:team-2"})
print(json.dumps(result))
"""


def account_token(account):
    token = jwt({"exp": int(time.time()) + 10 * 86400, "email": f"{account}@example.com",
                 "https://api.openai.com/auth": {"chatgpt_account_id": "acct-e2e", "chatgpt_user_id": f"user-{account}"}})
    # Seats of one Team workspace: same account id, different people.
    return {"access_token": token, "id_token": token, "refresh_token": f"refresh-{account}",
            "account_id": "acct-e2e"}


def several_accounts(directory):
    """Each deployment uses the token file of the subscription named in its api_key."""
    root = Path(directory)
    files = {"default": root / "auth.json", "team-2": root / "accounts/team-2/auth.json"}
    tokens = {}
    for account, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        record = account_token(account)
        path.write_text(json.dumps(record))
        tokens[record["access_token"]] = account
    env = {**os.environ, "CHATGPT_TOKEN_DIR": directory, "CHATGPT_ACCOUNTS": "default,team-2,team-3"}

    output = subprocess.run([sys.executable, "-c", ACCOUNT_CHILD], env=env, capture_output=True, text=True, timeout=60)
    found = json.loads(output.stdout or "{}")
    ok = check("api_key selects the token file",
               found.get("chatgpt-account:team-2") == ["team-2", str(files["team-2"])], found or output.stderr)
    ok &= check("a deployment without a subscription is rejected",
                all("No ChatGPT subscription" in found.get(key, "") for key in ("None", "sk-other")), found)
    ok &= check("unknown and invalid account names are rejected",
                "Unknown ChatGPT account 'nobody'" in found.get("chatgpt-account:nobody", "")
                and "GetAccessTokenError" in found.get("chatgpt-account:../x", ""), found)
    ok &= check("one authenticator per account", found.get("same") is True, found)

    config = root / "config.yaml"
    credentials = "credential_list:\n" + "".join(
        f"  - credential_name: chatgpt-{a}\n    credential_values:\n      api_key: chatgpt-account:{a}\n"
        f"    credential_info:\n      description: ChatGPT-Abo {a}\n"
        for a in ("default", "team-2", "team-3"))
    deployment = lambda name, model, credential=None, key=None: (
        f"  - model_name: {name}\n    model_info:\n      mode: responses\n"
        f"    litellm_params:\n      model: chatgpt/{model}\n"
        + (f"      litellm_credential_name: {credential}\n" if credential else "")
        + (f"      api_key: {key}\n" if key else ""))
    config.write_text(credentials + "model_list:\n" + deployment("multi-sub", "gpt-6-astra", "chatgpt-default")
                      + deployment("multi-sub", "gpt-6-astra", "chatgpt-team-2")
                      + deployment("unknown-sub", "gpt-6-astra", key="chatgpt-account:nobody")
                      + deployment("typo-sub", "gpt-6-astra", "chatgpt-tem-2")
                      + deployment("bare-sub", "gpt-6-astra")
                      + deployment("unsigned-sub", "gpt-6-astra", "chatgpt-team-3")
                      + f"router_settings:\n  routing_strategy: simple-shuffle\ngeneral_settings:\n  master_key: {KEY}\n")
    env.update({"CHATGPT_API_BASE": f"http://127.0.0.1:{BACKEND_PORT}", "CHATGPT_AUTH_BASE": f"http://127.0.0.1:{AUTH_PORT}",
                "CHATGPT_USAGE_URL": f"http://127.0.0.1:{BACKEND_PORT}/wham/usage",
                "CHATGPT_MODELS_URL": f"http://127.0.0.1:{BACKEND_PORT}/codex/models", "CHATGPT_MONITORING_API_KEY": MONITOR,
        "CHATGPT_DEVICE_POLL_MIN_SECONDS": "1", "CHATGPT_WATCH_INTERVAL_SECONDS": "1", "CHATGPT_AUTO_LOGIN": "false"})
    for name in ("DATABASE_URL", "STORE_MODEL_IN_DB"):
        env.pop(name, None)
    port = PROXY_PORT + 1
    log = open(root / "proxy.log", "w")
    proxy = subprocess.Popen(["litellm", "--config", str(config), "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        ready, _ = wait_for(lambda: request("GET", "/health/liveliness", port=port)[0], lambda s: s == 200, 120)
        ok &= check("proxy starts with several accounts", ready)
        Backend.seen.clear()
        failed = []
        for stream in (False, True):
            for _ in range(6):
                chat = {"model": "multi-sub", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
                responses = {"model": "multi-sub", "input": "hi", "stream": stream}
                for path, body in (("/v1/chat/completions", chat), ("/v1/responses", responses)):
                    status, answer = request("POST", path, KEY, body, port=port)
                    if status != 200:
                        failed.append((path, stream, status, answer))
        used = {}
        for token, account_id in Backend.seen:
            used.setdefault(tokens.get(token, "unknown"), set()).add(account_id)
        ok &= check("requests succeed on both accounts", not failed and len(Backend.seen) == 24, failed[:2] or len(Backend.seen))
        ok &= check("each deployment sends its own token and account id",
                    used == {"default": {"acct-e2e"}, "team-2": {"acct-e2e"}}, used)
        chat = {"model": "unknown-sub", "messages": [{"role": "user", "content": "hi"}]}
        status, body = request("POST", "/v1/chat/completions", KEY, chat, port=port)
        ok &= check("an unknown account fails with 401", status == 401 and "CHATGPT_ACCOUNTS" in str(body), f"{status}: {body}")
        for model in ("typo-sub", "bare-sub"):
            chat["model"] = model
            status, body = request("POST", "/v1/chat/completions", KEY, chat, port=port)
            ok &= check(f"{model}: a missing subscription fails with 401 instead of using default",
                        status == 401 and "No ChatGPT subscription" in str(body), f"{status}: {body}")
        chat["model"] = "unsigned-sub"
        status, body = request("POST", "/v1/chat/completions", KEY, chat, port=port)
        ok &= check("an account without sign-in fails with 401", status == 401 and "sign-in required" in str(body), f"{status}: {body}")
        kept = {account: tokens.get(json.loads(path.read_text())["access_token"]) for account, path in files.items()}
        ok &= check("each token file keeps its own token",
                    kept == {"default": "default", "team-2": "team-2"}
                    and not (root / "accounts/team-3/auth.json").exists(), kept)

        listed = request("GET", "/chatgpt/api/accounts", KEY, port=port)[1]
        by_name = {a["account"]: a for a in listed["accounts"]} if isinstance(listed, dict) else {}
        ok &= check("admin API lists every subscription with its models",
                    list(by_name) == ["default", "team-2", "team-3"]
                    and [by_name[n]["state"] for n in by_name] == ["ok", "ok", "login_required"]
                    and by_name["default"]["email"] == "default@example.com"
                    and by_name["default"]["models"] == ["multi-sub"] and by_name["default"]["credentials"] == ["chatgpt-default"]
                    and by_name["team-2"]["models"] == ["multi-sub"] and by_name["team-3"]["models"] == ["unsigned-sub"], listed)
        offered = lambda name: [(m["slug"], m["configured"]) for m in (by_name.get(name, {}).get("available_models") or {}).get("models", [])]
        ok &= check("the admin API lists the subscription's models and marks the configured ones",
                    offered("default") == [("gpt-6.1-sol", False), ("gpt-6-astra", True)] == offered("team-2")
                    and by_name["team-3"]["available_models"] is None, {n: offered(n) for n in by_name})
        problems = sorted(p["deployment"] for p in listed.get("problems", [])) if isinstance(listed, dict) else None
        ok &= check("the configuration check names every broken deployment",
                    problems == ["bare-sub", "typo-sub", "unknown-sub"], listed.get("problems") if isinstance(listed, dict) else listed)
        sensor = lambda name: request("GET", f"/chatgpt/accounts/{name}/usage/prtg?windows=primary", headers={"X-Monitoring-Key": MONITOR}, port=port)
        states = {name: sensor(name) for name in ("default", "team-3", "nobody")}
        ok &= check("PRTG alarms on a broken configuration, per subscription",
                    states["default"][1]["prtg"].get("error") == 1 and "configuration" in states["default"][1]["prtg"]["text"].lower()
                    and states["team-3"][1]["prtg"]["error"] == 1 and states["nobody"][0] == 404, states)
        overview = request("GET", "/chatgpt/accounts", headers={"X-Monitoring-Key": MONITOR}, port=port)
        rows = {a["account"]: a for a in overview[1]["accounts"]} if overview[0] == 200 else {}
        ok &= check("the monitoring overview lists subscriptions without personal data",
                    list(rows) == ["default", "team-2", "team-3"] and rows["team-2"]["credentials"] == ["chatgpt-team-2"]
                    and rows["default"]["state"] == "ok" and "@" not in json.dumps(overview[1])
                    and "acct-e2e" not in json.dumps(overview[1]) and len(overview[1]["problems"]) == 3, overview)

        Auth.email, Auth.confirmed = "default@example.com", True
        request("POST", "/chatgpt/api/accounts/team-3/login", KEY, port=port)
        refused, state = wait_for(lambda: request("GET", "/chatgpt/api/accounts", KEY, port=port)[1]["accounts"][2],
                                  lambda s: (s.get("error") or {}).get("code") == "duplicate_account")
        ok &= check("a person signed in to one subscription is refused for another",
                    refused and not state["token_valid"] and not (root / "accounts/team-3/auth.json").exists(), state)
        Auth.email = "team-3@example.com"
        request("POST", "/chatgpt/api/accounts/team-3/login", KEY, port=port)
        signed_in, state = wait_for(lambda: request("GET", "/chatgpt/api/accounts", KEY, port=port)[1]["accounts"][2],
                                    lambda s: s["state"] == "ok")
        ok &= check("another person signs in to the new subscription",
                    signed_in and state["email"] == "team-3@example.com", state)
        chat["model"] = "unsigned-sub"
        works, (status, body) = wait_for(lambda: request("POST", "/v1/chat/completions", KEY, chat, port=port), lambda r: r[0] == 200)
        ok &= check("its models work without a restart", works, f"{status}: {body}")
        ok &= check("the other subscriptions are unchanged",
                    {a: tokens.get(json.loads(path.read_text())["access_token"]) for a, path in files.items()}
                    == {"default": "default", "team-2": "team-2"})
        return ok
    finally:
        proxy.terminate()
        proxy.wait(timeout=30)
        log.close()


def main():
    import litellm.llms.chatgpt.common_utils as upstream
    from litellm.proxy.extensions.chatgpt_subscriptions import oauth

    for port, handler in ((AUTH_PORT, AuthHandler), (BACKEND_PORT, Backend)):
        threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever, daemon=True).start()
    ok = check("OAuth client id matches LiteLLM", oauth.CLIENT_ID == upstream.CHATGPT_CLIENT_ID)
    ok &= check("OAuth endpoints match LiteLLM", upstream.CHATGPT_OAUTH_TOKEN_URL == "https://auth.openai.com/oauth/token"
                and upstream.CHATGPT_DEVICE_CODE_URL.endswith("/api/accounts/deviceauth/usercode")
                and upstream.CHATGPT_DEVICE_TOKEN_URL.endswith("/api/accounts/deviceauth/token")
                and upstream.CHATGPT_DEVICE_VERIFY_URL == "https://auth.openai.com/codex/device")
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second, \
            tempfile.TemporaryDirectory() as third:
        ok &= concurrent_refresh(first)
        ok &= proxy_flow(second)
        ok &= several_accounts(third)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
