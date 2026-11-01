"""End-to-end check of the routing between several ChatGPT subscriptions.

Runs inside the built image without network access (run this script with Python). Exercises a synthetic routing configuration: a routing group
with cost-based routing, two subscription deployments with order 1 and a
paid deployment with order 2. A local backend answers with 429 for a
subscription whose quota is marked as used up. All tokens are fake.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fixture_helpers import FIXTURES, check, jwt, request, wait_for

BACKEND_PORT, PROXY_PORT = 18803, 14020
KEY = "sk-e2e-master"
COOLDOWN = 4  # model_info.cooldown_time of the subscription deployments
PAID = "sk-paid"


class Backend(BaseHTTPRequestHandler):
    """ChatGPT backend for the subscriptions and an OpenAI API for the paid deployment."""

    tokens = {}  # access token -> subscription
    exhausted = set()
    seen = []  # "default", "team-2", "paid"; with "!" for a 429

    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        bearer = self.headers.get("authorization", "")[len("Bearer "):]
        if bearer == PAID:
            Backend.seen.append("paid")
            return self.reply(200, {"id": "c-1", "object": "chat.completion", "created": int(time.time()),
                                    "model": "paid", "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": "paid"}}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        account = Backend.tokens.get(bearer, "unknown")
        if account in Backend.exhausted:
            Backend.seen.append(account + "!")
            # Shape as sent to Codex clients when the plan's quota is used up.
            return self.reply(429, {"error": {"type": "usage_limit_reached",
                                              "message": "The usage limit has been reached", "resets_in_seconds": 3600}})
        Backend.seen.append(account)
        self.send_response(200)
        self.end_headers()
        for event in deepcopy(FIXTURES["text"]):
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def sign_in(directory):
    for account, path in (("default", "auth.json"), ("team-2", "accounts/team-2/auth.json")):
        token = jwt({"exp": int(time.time()) + 10 * 86400, "email": f"{account}@example.com",
                     "https://api.openai.com/auth": {"chatgpt_account_id": "acct-e2e", "chatgpt_user_id": f"user-{account}"}})
        file = Path(directory, path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"access_token": token, "id_token": token, "refresh_token": f"r-{account}",
                                    "account_id": "acct-e2e"}))
        Backend.tokens[token] = account


CONFIG = f"""credential_list:
  - credential_name: chatgpt-default
    credential_values: {{api_key: "chatgpt-account:default"}}
    credential_info: {{custom_llm_provider: chatgpt, description: ChatGPT-Abo default}}
  - credential_name: chatgpt-team-2
    credential_values: {{api_key: "chatgpt-account:team-2"}}
    credential_info: {{custom_llm_provider: chatgpt, description: ChatGPT-Abo team-2}}
model_list:
  - model_name: sol-sub
    model_info: {{id: sub-default, cooldown_time: {COOLDOWN}}}
    litellm_params: {{model: chatgpt/gpt-5.6-sol, litellm_credential_name: chatgpt-default, order: 1}}
  - model_name: sol-sub
    model_info: {{id: sub-team-2, cooldown_time: {COOLDOWN}}}
    litellm_params: {{model: chatgpt/gpt-5.6-sol, litellm_credential_name: chatgpt-team-2, order: 1}}
  - model_name: sol-paid
    model_info: {{id: paid}}
    litellm_params: {{model: openai/gpt-paid, api_base: "http://127.0.0.1:{BACKEND_PORT}/v1", api_key: {PAID},
                      order: 2, input_cost_per_token: 0.000001, output_cost_per_token: 0.000002}}
litellm_settings:
  callbacks: ["e2e_probe.recorder"]
router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  allowed_fails: 3
  cooldown_time: 5
  routing_groups:
    - group_name: sol
      routing_strategy: cost-based-routing
      models: [sol-sub, sol-paid]
general_settings:
  master_key: {KEY}
"""


# Loaded by LiteLLM from the config directory; records each log entry's
# model after all logging hooks, as the spend logs receive it.
PROBE = """
import json, os
from litellm.integrations.custom_logger import CustomLogger

class Recorder(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        entry = kwargs.get("standard_logging_object") or {}
        with open(os.environ["E2E_LOGGED_MODELS"], "a") as handle:
            handle.write(json.dumps([entry.get("model_id"), entry.get("model")]) + "\\n")

recorder = Recorder()
"""


def ask(model="sol"):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    return request("POST", "/v1/chat/completions", KEY, body, port=PROXY_PORT)


def served(count, model="sol"):
    """Send requests one by one; return the client statuses and the backend calls."""
    Backend.seen.clear()
    statuses = [ask(model)[0] for _ in range(count)]
    return statuses, list(Backend.seen)


def main():
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", BACKEND_PORT), Backend).serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as directory:
        sign_in(directory)
        config = Path(directory, "config.yaml")
        config.write_text(CONFIG)
        Path(directory, "e2e_probe.py").write_text(PROBE)
        logged = Path(directory, "logged.jsonl")
        env = {**os.environ, "CHATGPT_TOKEN_DIR": directory, "CHATGPT_ACCOUNTS": "default,team-2",
               "E2E_LOGGED_MODELS": str(logged),
               "CHATGPT_API_BASE": f"http://127.0.0.1:{BACKEND_PORT}", "CHATGPT_AUTO_LOGIN": "false",
               "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY": "true"}
        for name in ("DATABASE_URL", "STORE_MODEL_IN_DB"):
            env.pop(name, None)
        log = open(Path(directory, "proxy.log"), "w")
        proxy = subprocess.Popen(["litellm", "--config", str(config), "--port", str(PROXY_PORT)],
                                 env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            ready, _ = wait_for(lambda: request("GET", "/health/liveliness", port=PROXY_PORT)[0], lambda s: s == 200, 120)
            ok = check("proxy starts", ready)

            statuses, seen = served(6)
            ok &= check("the group prefers the subscriptions over the paid deployment",
                        statuses == [200] * 6 and "paid" not in seen, (statuses, seen))
            statuses, direct = served(6, "sol-sub")
            ok &= check("the model name alone also stays on the subscriptions",
                        statuses == [200] * 6 and "paid" not in direct, (statuses, direct))
            # Same order, same (default) price: cost-based routing keeps the first one.
            print(f"        served while both are free: {seen + direct}", flush=True)

            Backend.exhausted = {"default"}
            statuses, seen = served(8)
            ok &= check("a used-up subscription does not fail the request", statuses == [200] * 8, (statuses, seen))
            ok &= check("the other subscription takes over, the used-up one rests during its cooldown",
                        seen.count("default!") <= 1 and seen.count("team-2") == 8 and "paid" not in seen, seen)

            Backend.exhausted = {"default", "team-2"}
            statuses, seen = served(4)
            ok &= check("with both subscriptions used up the paid deployment takes over",
                        statuses == [200] * 4 and seen.count("paid") == 4, (statuses, seen))
            status, body = ask("sol-sub")
            ok &= check("without a paid deployment the client gets 429", status == 429, f"{status}: {body}")

            Backend.exhausted = set()
            time.sleep(COOLDOWN + 1)
            statuses, seen = served(4)
            ok &= check("after the cooldown the subscriptions serve again",
                        statuses == [200] * 4 and "paid" not in seen and set(seen) <= {"default", "team-2"}, (statuses, seen))

            time.sleep(2)  # logging runs after the response
            names = {}
            for line in logged.read_text().splitlines() if logged.exists() else []:
                model_id, model = json.loads(line)
                names.setdefault(model_id, set()).add(model)
            ok &= check("the request log names the subscription in the model",
                        names.get("sub-default") == {"chatgpt/default/gpt-5.6-sol"}
                        and names.get("sub-team-2") == {"chatgpt/team-2/gpt-5.6-sol"}
                        and "chatgpt/" not in str(names.get("paid")), names)
            sys.exit(0 if ok else 1)
        finally:
            proxy.terminate()
            proxy.wait(timeout=30)
            log.close()


if __name__ == "__main__":
    main()
