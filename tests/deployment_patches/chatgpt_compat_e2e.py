"""End-to-end check of the ChatGPT provider with standard OpenAI clients.

Runs inside the built image (run this script with Python). A local
backend emits synthetic ChatGPT SSE streams and enforces the backend's input
rules. The proxy runs without a database, with fake credentials in a temporary
token directory. No real token and no subscription quota are used.
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_PORT = 18765
PROXY_PORT = 14000
KEY = "sk-e2e"
MODEL = "gpt-5.6-sol-sub"
CATALOG_MODEL = "catalog-only-sub"
CATALOG_SLUG = "gpt-6.1-catalog-only-synthetic"
CALLS = []
FIXTURES = json.loads((Path(__file__).parent / "fixtures/chatgpt_sse.json").read_text())
REQUESTS = []


def texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for entry in value:
            yield from texts(entry)
    elif isinstance(value, dict):
        for key in ("text", "content"):
            if key in value:
                yield from texts(value[key])


class Backend(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        REQUESTS.append(body)
        CALLS.append({"path": self.path, "model": body.get("model"),
                      "account": self.headers.get("chatgpt-account-id")})
        if self.path == "/v1/chat/completions" and self.headers.get("authorization") == "Bearer sk-openai-fixture":
            return self.reply(200, {"id": "chat-native", "object": "chat.completion", "created": 1,
                                    "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": "native-openai"}}]})
        if self.path != "/responses":
            return self.reply(403, {"detail": "The subscription backend only supports /responses"})
        encoded = self.headers.get("authorization", "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if claims["https://api.openai.com/auth"]["chatgpt_account_id"] != self.headers.get("chatgpt-account-id"):
            return self.reply(401, {"detail": "Subscription identity does not match its credential"})
        if not isinstance(body.get("input"), list):
            return self.reply(400, {"detail": "Input must be a list"})
        if body.get("stream") is not True:
            return self.reply(400, {"detail": "Stream must be set to true"})
        events = deepcopy(FIXTURES["tool" if body.get("tools") else "text"])
        if any("EMPTY" in text for text in texts(body["input"])):
            for event in events:
                if event["type"] == "response.completed":
                    event["response"]["output"] = []
        # Like the real backend: SSE without a content-type header.
        self.send_response(200)
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start():
    ThreadingHTTPServer(("127.0.0.1", BACKEND_PORT), Backend).serve_forever()


def fake_credentials(directory, identity="e2e"):
    encode = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    token = "e30." + encode({
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": identity},
    }) + ".sig"
    (Path(directory) / "auth.json").write_text(json.dumps(
        {"access_token": token, "refresh_token": "e2e", "id_token": token}
    ))


def start_proxy(directory):
    config = Path(directory) / "config.yaml"
    config.write_text(
        "credential_list:\n  - credential_name: chatgpt-default\n"
        "    credential_values:\n      api_key: chatgpt-account:default\n"
        "    credential_info:\n      description: ChatGPT-Abo default\n"
        f"model_list:\n  - model_name: {MODEL}\n    model_info:\n      mode: responses\n"
        "    litellm_params:\n      model: chatgpt/gpt-5.6-sol\n      litellm_credential_name: chatgpt-default\n"
        f"  - model_name: {CATALOG_MODEL}\n    model_info: {{mode: chat}}\n"
        f"    litellm_params:\n      model: chatgpt/{CATALOG_SLUG}\n"
        "      custom_llm_provider: chatgpt\n      api_key: chatgpt-account:catalog-only\n"
        "  - model_name: native-openai\n    litellm_params:\n"
        "      model: openai/synthetic-native-chat-model\n      custom_llm_provider: openai\n"
        f"      api_base: http://127.0.0.1:{BACKEND_PORT}/v1\n      api_key: sk-openai-fixture\n"
        f"general_settings:\n  master_key: {KEY}\n"
    )
    env = {
        **os.environ,
        "CHATGPT_TOKEN_DIR": directory,
        "CHATGPT_ACCOUNTS": "default,catalog-only",
        "CHATGPT_AUTH_FILE": "auth.json",
        "CHATGPT_AUTO_LOGIN": "false",
        "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY": "true",
        "CHATGPT_API_BASE": f"http://127.0.0.1:{BACKEND_PORT}",
    }
    for name in ("DATABASE_URL", "STORE_MODEL_IN_DB"):
        env.pop(name, None)
    log = open(Path(directory) / "proxy.log", "w")
    process = subprocess.Popen(
        ["litellm", "--config", str(config), "--port", str(PROXY_PORT)],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/health/liveliness", timeout=2)
            return process
        except OSError:
            if process.poll() is not None:
                break
            time.sleep(1)
    process.kill()
    print((Path(directory) / "proxy.log").read_text()[-3000:])
    raise SystemExit("Proxy did not start.")


def tool():
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }


RESPONSES_TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "Get weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}


def check_tool_call(name, arguments):
    assert name == "get_weather", name
    assert json.loads(arguments) == {"city": "Berlin"}, arguments


def cases(client, model=MODEL):
    user = [{"role": "user", "content": "Sag nur: ok"}]
    weather = [{"role": "user", "content": "Wie ist das Wetter in Berlin?"}]

    def chat_text():
        r = client.chat.completions.create(model=model, messages=user)
        assert r.choices[0].message.content == "ok", r

    def chat_text_empty_final_event():
        r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "EMPTY"}])
        assert r.choices[0].message.content == "ok", r

    def chat_tool():
        r = client.chat.completions.create(model=model, messages=weather, tools=[tool()])
        call = r.choices[0].message.tool_calls[0]
        check_tool_call(call.function.name, call.function.arguments)
        assert r.choices[0].finish_reason == "tool_calls", r.choices[0].finish_reason

    def chat_stream_text():
        chunks = client.chat.completions.create(model=model, messages=user, stream=True)
        assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "ok"

    def chat_stream_tool():
        name, arguments = None, ""
        for chunk in client.chat.completions.create(model=model, messages=weather, tools=[tool()], stream=True):
            for call in (chunk.choices[0].delta.tool_calls or []) if chunk.choices else []:
                name = call.function.name or name
                arguments += call.function.arguments or ""
        check_tool_call(name, arguments)

    def chat_reasoning_effort():
        REQUESTS.clear()
        client.chat.completions.create(model=model, messages=user, reasoning_effort="high")
        assert REQUESTS[-1]["reasoning"]["effort"] == "high", REQUESTS[-1].get("reasoning")

    def responses_string_input():
        r = client.responses.create(model=model, input="Sag nur: ok")
        assert r.output_text == "ok", r

    def responses_tool():
        r = client.responses.create(model=model, input="Wie ist das Wetter in Berlin?", tools=[RESPONSES_TOOL])
        call = next(o for o in r.output if o.type == "function_call")
        check_tool_call(call.name, call.arguments)

    def responses_stream_final_response():
        with client.responses.stream(model=model, input="Wie ist das Wetter in Berlin?", tools=[RESPONSES_TOOL]) as stream:
            for _ in stream:
                pass
            final = stream.get_final_response()
        call = next(o for o in final.output if o.type == "function_call")
        check_tool_call(call.name, call.arguments)

    weather_list = [{"role": "user", "content": [{"type": "input_text", "text": "Wie ist das Wetter in Berlin?"}]}]

    def responses_list_input_json():
        r = client.responses.create(model=model, input=weather_list, tools=[RESPONSES_TOOL])
        call = next(o for o in r.output if o.type == "function_call")
        check_tool_call(call.name, call.arguments)

    def responses_list_input_stream_final_response():
        with client.responses.stream(model=model, input=weather_list, tools=[RESPONSES_TOOL]) as stream:
            for _ in stream:
                pass
            final = stream.get_final_response()
        call = next(o for o in final.output if o.type == "function_call")
        check_tool_call(call.name, call.arguments)

    return [
        chat_text, chat_text_empty_final_event, chat_tool, chat_stream_text,
        chat_stream_tool, chat_reasoning_effort, responses_string_input,
        responses_tool, responses_stream_final_response,
        responses_list_input_json, responses_list_input_stream_final_response,
    ]


def main():
    from openai import OpenAI

    threading.Thread(target=start, daemon=True).start()
    temporary = tempfile.TemporaryDirectory()
    directory = temporary.name
    fake_credentials(directory)
    catalog_directory = Path(directory) / "accounts/catalog-only"
    catalog_directory.mkdir(parents=True)
    fake_credentials(catalog_directory, "e2e-catalog-only")
    os.environ.update({
        "CHATGPT_TOKEN_DIR": directory,
        "CHATGPT_ACCOUNTS": "default,catalog-only",
        "CHATGPT_API_BASE": f"http://127.0.0.1:{BACKEND_PORT}",
        "CHATGPT_AUTO_LOGIN": "false",
    })
    proxy = start_proxy(directory)
    client = OpenAI(base_url=f"http://127.0.0.1:{PROXY_PORT}/v1", api_key=KEY, max_retries=0, timeout=60)
    def direct_sync_responses_json():
        import litellm
        response = litellm.responses(model="chatgpt/gpt-5.6-sol", input="Say ok", api_key="chatgpt-account:default")
        assert response.output[0].content[0].text == "ok"

    def direct_async_responses_json():
        import litellm
        response = asyncio.run(litellm.aresponses(model="chatgpt/gpt-5.6-sol", input="Say ok", api_key="chatgpt-account:default"))
        assert response.output[0].content[0].text == "ok"

    def direct_sync_extra_body_stream():
        import litellm
        response = litellm.responses(model="chatgpt/gpt-5.6-sol", input="Say ok",
                                     api_key="chatgpt-account:default", extra_body={"stream": True})
        completed = [event for event in response if event.type == "response.completed"]
        assert len(completed) == 1
        assert completed[0].response.output[0].content[0].text == "ok"

    def direct_async_extra_body_stream():
        import litellm
        async def collect():
            response = await litellm.aresponses(model="chatgpt/gpt-5.6-sol", input="Say ok",
                                               api_key="chatgpt-account:default", extra_body={"stream": True})
            return [event async for event in response if event.type == "response.completed"]
        completed = asyncio.run(collect())
        assert len(completed) == 1
        assert completed[0].response.output[0].content[0].text == "ok"

    def native_openai_chat_stays_native():
        CALLS.clear()
        response = client.chat.completions.create(model="native-openai", messages=[{"role": "user", "content": "Say ok"}])
        assert response.choices[0].message.content == "native-openai"
        assert CALLS == [{"path": "/v1/chat/completions", "model": "synthetic-native-chat-model", "account": None}], CALLS

    failed = 0
    try:
        for case in [*cases(client), direct_sync_responses_json, direct_async_responses_json,
                     direct_sync_extra_body_stream, direct_async_extra_body_stream, native_openai_chat_stays_native]:
            try:
                case()
                print(f"ok      {case.__name__}")
            except Exception as exc:  # noqa: BLE001 - report every case
                failed += 1
                print(f"FAILED  {case.__name__}: {type(exc).__name__}: {str(exc)[:300]}")
        for case in cases(client, CATALOG_MODEL):
            try:
                CALLS.clear()
                case()
                assert CALLS and all(call == {"path": "/responses", "model": CATALOG_SLUG, "account": "e2e-catalog-only"} for call in CALLS), CALLS
                print(f"ok      unlisted_{case.__name__}")
            except Exception as exc:  # noqa: BLE001 - report every case
                failed += 1
                print(f"FAILED  unlisted_{case.__name__}: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        proxy.terminate()
        proxy.wait(timeout=30)
        temporary.cleanup()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
