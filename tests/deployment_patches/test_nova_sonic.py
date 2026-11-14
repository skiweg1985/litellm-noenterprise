"""Nova Sonic extension tests against the installed package; no network."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from litellm.proxy.extensions.nova_sonic import accounting, policy, relay
from litellm.proxy.extensions.nova_sonic.policy import SonicError

PRICES = {
    "input_cost_per_audio_token": 0.000003,
    "input_cost_per_token": 0.00000033,
    "output_cost_per_audio_token": 0.000012,
    "output_cost_per_token": 0.00000275,
}
TOOL = {"toolSpec": {"name": "case_lookup", "description": "Look up the case", "inputSchema": {"json": "{}"}}}


def event(name, body=None):
    return {"event": {name: {} if body is None else body}}


def usage(in_speech, in_text, out_speech, out_text):
    return event("usageEvent", {
        "details": {"total": {
            "input": {"speechTokens": in_speech, "textTokens": in_text},
            "output": {"speechTokens": out_speech, "textTokens": out_text},
        }},
        "totalInputTokens": in_speech + in_text,
        "totalOutputTokens": out_speech + out_text,
    })


OPENING = [
    event("sessionStart", {"inferenceConfiguration": {"maxTokens": 1024}}),
    event("promptStart", {"promptName": "p", "toolConfiguration": {"tools": [TOOL]}}),
    event("contentStart", {"promptName": "p", "contentName": "mic", "type": "AUDIO", "role": "USER"}),
]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeLog:
    def __init__(self, litellm_params=None):
        self.model_call_details = {"litellm_params": litellm_params or {"metadata": {}}}
        self.success = []
        self.failure = []
        self.completion_start = []

    def update_completion_start_time(self, value):
        self.completion_start.append(value)

    async def dispatch_success_handlers(self, **kwargs):
        self.success.append(kwargs)

    async def dispatch_failure_handlers(self, error, traceback, prefer_async_handlers=False):
        self.failure.append(error)


class FakeClient:
    def __init__(self, messages=()):
        self.inbox = asyncio.Queue()
        for message in messages:
            self.inbox.put_nowait(message)
        self.sent = []
        self.closed = None

    def text(self, frame):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})

    def disconnect(self):
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


class FakeStream:
    """Bedrock's bidirectional stream. After sessionEnd it reports usage and ends, like Bedrock."""

    def __init__(self, events=(), final_usage=None, refuse=None):
        self.output = asyncio.Queue()
        for item in events:
            self.push(item)
        self.sent = []
        self.closed = False
        self.final_usage = final_usage
        self.refuse = refuse
        self.input_stream = self

    def push(self, item):
        self.output.put_nowait(item)

    async def send(self, chunk):
        frame = json.loads(chunk)
        self.sent.append(frame)
        if "sessionEnd" in frame["event"]:
            if self.final_usage is not None:
                self.push(self.final_usage)
            self.push(None)

    async def close(self):
        self.closed = True

    async def await_output(self):
        if self.refuse is not None:
            raise self.refuse
        return None, self

    async def receive(self):
        item = await self.output.get()
        if isinstance(item, BaseException):
            raise item
        if item is None:
            return None
        return SimpleNamespace(value=SimpleNamespace(bytes_=json.dumps(item).encode()))

    def names(self):
        return [next(iter(frame["event"])) for frame in self.sent]


class ValidationException(Exception):
    pass


def raw(chunk):
    return chunk


def session(log=None, clock=None):
    return accounting.SessionAccounting(log or FakeLog(), "nova-sonic", "amazon.nova-2-5-sonic", PRICES,
                                        clock=clock or Clock())


class PolicyTests(unittest.TestCase):
    def test_parse_returns_the_one_event(self):
        self.assertEqual(("audioInput", {"content": "AAAA"}),
                         policy.parse_frame(json.dumps(event("audioInput", {"content": "AAAA"}))))

    def test_parse_rejects_binary_duplicates_shapes_and_large_frames(self):
        cases = [
            (None, 1003),
            ('{"event": {"audioInput": {}}, "event": {}}', 1007),
            ("[]", 1007),
            ('{"event": {"a": {}, "b": {}}}', 1007),
            ('{"event": {"sessionEnd": {}}, "type": "x"}', 1007),
            ('{"event": {"sessionEnd": []}}', 1007),
            ("{", 1007),
            (json.dumps(event("audioInput", {"content": "A" * (256 * 1024)})), 1009),
        ]
        for text, code in cases:
            with self.assertRaises(SonicError, msg=text) as ctx:
                policy.parse_frame(text)
            self.assertEqual(code, ctx.exception.code)
            self.assertTrue(ctx.exception.fatal)

    def test_client_events(self):
        for name in policy.CLIENT_EVENTS:
            policy.check_client_event(name, {})
        with self.assertRaises(SonicError) as ctx:
            policy.check_client_event("usageEvent", {})
        self.assertFalse(ctx.exception.fatal)

    def test_only_client_tools(self):
        policy.check_client_event("promptStart", {"toolConfiguration": {"tools": [TOOL], "toolChoice": {"auto": {}}}})
        policy.check_client_event("promptStart", {"toolConfiguration": {"toolChoice": {"tool": {"name": "x"}}}})
        rejected = [
            {"tools": [{"systemTool": {"name": "web"}}]},
            {"tools": [{"toolSpec": {"name": "x", "endpoint": "https://example.test"}}]},
            {"tools": [{"toolSpec": {"description": "no name"}}]},
            {"tools": [TOOL], "mcpServers": []},
            {"tools": {"toolSpec": {}}},
            {"toolChoice": {"required": {}}},
            "tools",
        ]
        for configuration in rejected:
            with self.assertRaises(SonicError, msg=configuration) as ctx:
                policy.check_client_event("promptStart", {"toolConfiguration": configuration})
            self.assertFalse(ctx.exception.fatal)

    def test_query_model(self):
        self.assertEqual("nova-sonic", policy.query_model({"model": "nova-sonic"}))
        with self.assertRaises(SonicError):
            policy.query_model({})

    def test_error_frame_is_not_a_bedrock_event(self):
        frame = json.loads(policy.error_frame("nope"))
        self.assertEqual({"error": {"type": "invalid_request_error", "message": "nope"}}, frame)


class AccountingTests(unittest.TestCase):
    def test_prices_prefer_deployment_pricing(self):
        params = {"input_cost_per_audio_token": 0.1, "model_info": {"output_cost_per_token": 0.2}}
        prices = accounting.resolve_prices(params, "amazon.nova-2-5-sonic", lambda name: PRICES)
        self.assertEqual(0.1, prices["input_cost_per_audio_token"])
        self.assertEqual(0.2, prices["output_cost_per_token"])
        self.assertEqual(PRICES["output_cost_per_audio_token"], prices["output_cost_per_audio_token"])

    def test_prices_use_base_model_for_other_names(self):
        seen = []

        def lookup(name):
            seen.append(name)
            if name != "amazon.nova-2-5-sonic":
                raise ValueError(name)
            return PRICES

        prices = accounting.resolve_prices({"base_model": "bedrock/amazon.nova-2-5-sonic"}, "my-sonic", lookup)
        self.assertEqual(PRICES, prices)
        self.assertEqual(["amazon.nova-2-5-sonic"], seen)

    def test_prices_from_the_installed_price_list(self):
        prices = accounting.resolve_prices({}, "amazon.nova-2-5-sonic")
        self.assertEqual(set(accounting.PRICE_FIELDS.values()), set(prices))
        self.assertTrue(all(price > 0 for price in prices.values()))

    def test_missing_or_negative_price_refuses_the_session(self):
        with self.assertRaises(SonicError) as ctx:
            accounting.resolve_prices({}, "unknown", lambda name: {})
        self.assertEqual(1011, ctx.exception.code)
        with self.assertRaises(SonicError):
            accounting.resolve_prices({"input_cost_per_token": -1}, "amazon.nova-2-5-sonic", lambda name: PRICES)

    def test_usage_is_cumulative(self):
        acc = session()
        acc.received("usageEvent", usage(100, 10, 50, 5)["event"]["usageEvent"])
        acc.received("usageEvent", usage(300, 20, 200, 15)["event"]["usageEvent"])
        acc.received("usageEvent", {"details": {"total": {"input": {"speechTokens": -1}}}})
        expected = 300 * 0.000003 + 20 * 0.00000033 + 200 * 0.000012 + 15 * 0.00000275
        self.assertAlmostEqual(expected, acc.cost())
        self.assertEqual((320, 215), (acc.total_input_tokens, acc.total_output_tokens))

    def test_finish_bills_once_with_token_usage(self):
        log = FakeLog()
        acc = session(log)
        acc.accepted = True
        acc.received("toolUse", {"toolUseId": "t"})
        acc.received("audioOutput", {"content": "AAAA"})
        acc.received("usageEvent", usage(1000, 0, 500, 0)["event"]["usageEvent"])

        class Result:
            pass

        def factory(input_tokens, output_tokens):
            result = Result()
            result.tokens = (input_tokens, output_tokens)
            return result

        asyncio.run(acc.finish(result_factory=factory, success_key="ok", failure_key="failed"))
        asyncio.run(acc.finish(result_factory=factory, success_key="ok", failure_key="failed"))
        self.assertEqual(1, len(log.success))
        result = log.success[0]["result"]
        self.assertEqual((1000, 500), result.tokens)
        self.assertAlmostEqual(1000 * 0.000003 + 500 * 0.000012, result._hidden_params["response_cost"])
        summary = log.model_call_details["litellm_params"]["metadata"]["spend_logs_metadata"]["nova_sonic"]
        self.assertEqual(1, summary["tool_uses"])
        self.assertEqual(1000, summary["input_speech_tokens"])
        self.assertEqual(1, len(log.completion_start))

    def test_finish_logs_refused_stream_as_failure(self):
        log = FakeLog()
        acc = session(log)
        acc.end_reason = "provider_error"
        asyncio.run(acc.finish(success_key="ok", failure_key="failed"))
        self.assertEqual([], log.success)
        self.assertEqual(1, len(log.failure))
        self.assertTrue(log.model_call_details["failed"])


class RelayTests(unittest.TestCase):
    def run_relay(self, client, stream):
        acc = session()
        reason = asyncio.run(asyncio.wait_for(relay.relay(client, stream, acc, raw), 10))
        return reason, acc

    def test_conversation_with_a_client_tool(self):
        client = FakeClient()
        stream = FakeStream([
            event("completionStart", {"sessionId": "s1", "promptName": "p", "completionId": "c"}),
            event("toolUse", {"toolUseId": "t1", "toolName": "case_lookup", "content": "{}"}),
            event("audioOutput", {"content": "AAAA"}),
        ], final_usage=usage(100, 20, 50, 10))
        for frame in OPENING:
            client.text(frame)
        client.text(event("audioInput", {"promptName": "p", "contentName": "mic", "content": "AAAA"}))
        client.text(event("contentStart", {"promptName": "p", "contentName": "r", "type": "TOOL", "role": "TOOL"}))
        client.text(event("toolResult", {"promptName": "p", "contentName": "r", "content": "{\"ok\": true}"}))
        client.text(event("contentEnd", {"promptName": "p", "contentName": "r"}))
        client.text(event("contentEnd", {"promptName": "p", "contentName": "mic"}))
        client.text(event("promptEnd", {"promptName": "p"}))
        client.text(event("sessionEnd"))
        reason, acc = self.run_relay(client, stream)
        self.assertEqual("client_close", reason)
        self.assertEqual(["sessionStart", "promptStart", "contentStart", "audioInput", "contentStart",
                          "toolResult", "contentEnd", "contentEnd", "promptEnd", "sessionEnd"], stream.names())
        self.assertEqual(["completionStart", "toolUse", "audioOutput", "usageEvent"],
                         [next(iter(frame["event"])) for frame in client.sent])
        self.assertTrue(acc.accepted)
        self.assertEqual(1, acc.tool_uses)
        self.assertEqual("s1", acc.provider_session_id)
        self.assertEqual(100, acc.tokens[("input", "speechTokens")])

    def test_unsupported_event_is_answered_and_dropped(self):
        client = FakeClient()
        stream = FakeStream()
        client.text(event("sessionStart"))
        client.text(event("response.create"))
        client.text(event("promptStart", {"promptName": "p", "toolConfiguration": {"tools": [{"bedrockTool": {}}]}}))
        client.text(event("sessionEnd"))
        reason, _ = self.run_relay(client, stream)
        self.assertEqual("client_close", reason)
        self.assertEqual(["sessionStart", "sessionEnd"], stream.names())
        errors = [frame["error"]["message"] for frame in client.sent if "error" in frame]
        self.assertIn("response.create", errors[0])
        self.assertIn("client tools", errors[1])

    def test_disconnect_closes_what_the_client_left_open(self):
        client = FakeClient()
        stream = FakeStream(final_usage=usage(10, 0, 5, 0))
        for frame in OPENING:
            client.text(frame)
        client.disconnect()
        reason, acc = self.run_relay(client, stream)
        self.assertEqual("client_disconnect", reason)
        self.assertEqual(["sessionStart", "promptStart", "contentStart", "contentEnd", "promptEnd", "sessionEnd"],
                         stream.names())
        self.assertEqual({"promptName": "p", "contentName": "mic"}, stream.sent[3]["event"]["contentEnd"])
        self.assertTrue(stream.closed)
        self.assertEqual(10, acc.tokens[("input", "speechTokens")])

    def test_disconnect_before_session_start_sends_nothing(self):
        client = FakeClient()
        stream = FakeStream()
        client.disconnect()
        reason, _ = self.run_relay(client, stream)
        self.assertEqual("client_disconnect", reason)
        self.assertEqual([], stream.sent)

    def test_bedrock_ending_the_stream(self):
        client = FakeClient()
        stream = FakeStream([usage(5, 0, 5, 0), None])
        client.text(OPENING[0])
        reason, acc = self.run_relay(client, stream)
        self.assertEqual("provider_closed", reason)
        self.assertTrue(acc.usage_reported)

    def test_bedrock_error_propagates(self):
        client = FakeClient()
        stream = FakeStream([ValidationException("Timed out waiting for input events")])
        with self.assertRaises(ValidationException):
            self.run_relay(client, stream)

    def test_binary_client_frame_ends_the_session(self):
        client = FakeClient()
        for frame in OPENING[:2]:
            client.text(frame)
        client.inbox.put_nowait({"type": "websocket.receive", "bytes": b"\x00"})
        stream = FakeStream()
        with self.assertRaises(SonicError) as ctx:
            self.run_relay(client, stream)
        self.assertEqual(1003, ctx.exception.code)
        self.assertEqual(["sessionStart", "promptStart", "promptEnd", "sessionEnd"], stream.names())


class RunBedrockTests(unittest.TestCase):
    SECRET = "wJalrXUtnFEMI-geheim-1234567890"

    def run_bedrock(self, client, stream, log=None):
        log = log or FakeLog({"metadata": {}, **PRICES})

        async def open_stream():
            return stream

        with patch.object(accounting.SessionAccounting, "finish", AsyncMock()) as finish:
            asyncio.run(asyncio.wait_for(relay.run_bedrock(
                client, model="amazon.nova-2-5-sonic", open_stream=open_stream, logging_obj=log,
                context={"alias": "nova-sonic"}, secrets=("AKIAEXAMPLE", self.SECRET), chunk=raw), 10))
        return finish

    def test_bills_and_closes_the_client(self):
        client = FakeClient()
        stream = FakeStream(final_usage=usage(10, 0, 5, 0))
        client.text(OPENING[0])
        client.text(event("sessionEnd"))
        finish = self.run_bedrock(client, stream)
        finish.assert_awaited_once()
        self.assertEqual((1000, "Nova Sonic session ended."), client.closed)
        self.assertTrue(stream.closed)

    def test_bedrock_error_keeps_its_type_without_secrets(self):
        client = FakeClient()
        stream = FakeStream([ValidationException(f"Bad signature for {self.SECRET}")])
        self.run_bedrock(client, stream)
        error = client.sent[-1]["error"]
        self.assertEqual("ValidationException", error["type"])
        self.assertNotIn(self.SECRET, error["message"])
        self.assertEqual(1011, client.closed[0])
        self.assertIn("ValidationException", client.closed[1])

    def test_refused_stream(self):
        client = FakeClient()
        log = FakeLog({"metadata": {}, **PRICES})

        class AccessDeniedException(Exception):
            pass

        stream = FakeStream(refuse=AccessDeniedException("not allowed"))
        client.text(OPENING[0])

        async def open_stream():
            return stream

        asyncio.run(asyncio.wait_for(relay.run_bedrock(
            client, model="amazon.nova-2-5-sonic", open_stream=open_stream, logging_obj=log,
            context={"alias": "nova-sonic"}, chunk=raw), 10))
        self.assertEqual("AccessDeniedException", client.sent[-1]["error"]["type"])
        # Logged as a failure without cost.
        self.assertEqual([], log.success)
        self.assertEqual(1, len(log.failure))

    def test_missing_price_refuses_before_opening_the_stream(self):
        client = FakeClient()
        opened = AsyncMock()
        with patch("litellm.get_model_info", side_effect=ValueError("unknown")):
            asyncio.run(relay.run_bedrock(
                client, model="custom-sonic", open_stream=opened, logging_obj=FakeLog(),
                context={"alias": "nova-sonic"}, chunk=raw))
        opened.assert_not_awaited()
        self.assertEqual(1011, client.closed[0])
        self.assertIn("No price", client.sent[-1]["error"]["message"])


class RouteTests(unittest.TestCase):
    def test_is_bedrock(self):
        from litellm.proxy.extensions.nova_sonic.routes import is_bedrock

        self.assertTrue(is_bedrock({"litellm_params": {"model": "bedrock/amazon.nova-2-5-sonic"}}))
        self.assertTrue(is_bedrock({"litellm_params": {"model": "amazon.nova-2-5-sonic",
                                                       "custom_llm_provider": "bedrock"}}))
        self.assertFalse(is_bedrock({"litellm_params": {"model": "azure/gpt-realtime"}}))
        self.assertFalse(is_bedrock({"litellm_params": {"model": "bedrock/x", "custom_llm_provider": "openai"}}))


class IntegrationTests(unittest.TestCase):
    def test_routes_are_llm_api_routes(self):
        from litellm.proxy._types import LiteLLMRoutes
        from litellm.proxy.auth.route_checks import RouteChecks

        for path in policy.PATHS:
            self.assertIn(path, LiteLLMRoutes.openai_routes.value)
            # The same check admits OIDC provider tokens to model APIs.
            self.assertTrue(RouteChecks.is_llm_api_route(path))

    def test_proxy_registers_the_routes_once(self):
        from litellm.proxy.proxy_server import app

        from starlette.routing import WebSocketRoute

        paths = [route.path for route in app.routes if isinstance(route, WebSocketRoute)]
        for path in policy.PATHS:
            self.assertEqual(1, paths.count(path), path)

    def call_handler(self, scope):
        from litellm.llms.bedrock.realtime.handler import BedrockRealtime

        frozen = SimpleNamespace(access_key="AKIAEXAMPLEKEY", secret_key="s" * 40, token="t" * 20)
        credentials = SimpleNamespace(get_frozen_credentials=lambda: frozen)

        async def signing(function, *args):
            return credentials if args else function()

        socket = SimpleNamespace(scope=scope)
        handler = BedrockRealtime()
        with patch("litellm.llms.bedrock.realtime.handler.run_aws_signing", new=signing), \
                patch("litellm.proxy.extensions.nova_sonic.relay.run_bedrock", new=AsyncMock()) as run, \
                patch.object(BedrockRealtime, "_run_session", new=AsyncMock()) as translated:
            asyncio.run(handler.async_realtime(
                model="amazon.nova-2-5-sonic", websocket=socket, logging_obj=FakeLog(),
                aws_region_name="eu-north-1", aws_access_key_id="AKIAEXAMPLEKEY", aws_secret_access_key="s" * 40))
        return socket, run, translated

    def test_bedrock_handler_hands_marked_sessions_to_the_relay(self):
        socket, run, translated = self.call_handler({policy.SCOPE_KEY: {"alias": "nova-sonic"}})
        run.assert_awaited_once()
        translated.assert_not_awaited()
        self.assertIs(socket, run.call_args.args[0])
        self.assertEqual("amazon.nova-2-5-sonic", run.call_args.kwargs["model"])
        self.assertEqual("nova-sonic", run.call_args.kwargs["context"]["alias"])
        self.assertIn("s" * 40, run.call_args.kwargs["secrets"])
        self.assertTrue(callable(run.call_args.kwargs["open_stream"]))

    def test_unmarked_sessions_keep_the_realtime_path(self):
        _, run, translated = self.call_handler({})
        run.assert_not_awaited()
        translated.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
