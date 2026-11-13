"""GPT-Live extension tests against the installed package; no network."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from litellm.proxy.extensions.gpt_live import accounting, policy, relay
from litellm.proxy.extensions.gpt_live.policy import LiveError

START = {"type": "session.start", "session": {"model": "gpt-live", "instructions": "Deutsch"}}


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


class Result:
    pass


class FakeClient:
    def __init__(self, messages=()):
        self.inbox = asyncio.Queue()
        for message in messages:
            self.inbox.put_nowait(message)
        self.sent = []
        self.closed = None

    def text(self, event):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(event)})

    def disconnect(self):
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


class FakeUpstream:
    """Answers session.close with session.closed, like Azure."""

    def __init__(self, events=(), closed_seconds=20.0):
        self.queue = asyncio.Queue()
        for event in events:
            self.queue.put_nowait(json.dumps(event))
        self.sent = []
        self.closed_seconds = closed_seconds

    async def send(self, text):
        self.sent.append(json.loads(text))
        if json.loads(text)["type"] == "session.close" and self.closed_seconds is not None:
            self.queue.put_nowait(json.dumps(
                {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": self.closed_seconds}}
            ))

    def push(self, event):
        self.queue.put_nowait(event if isinstance(event, (str, bytes)) else json.dumps(event))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def session(clock=None, log=None):
    return accounting.SessionAccounting(log or FakeLog(), "gpt-live", "gpt-live-1", 0.001, clock=clock or Clock())


class PolicyTests(unittest.TestCase):
    def test_start_accepts_client_delegation_only(self):
        self.assertEqual("gpt-live", policy.start_model(START))
        for delegation in (None, {"type": "client"}, {}):
            event = {"type": "session.start", "session": {"model": "m", "delegation": delegation}}
            self.assertEqual("m", policy.start_model(event))
        for delegation in ({"type": "responses", "responses": {"model": "x"}}, "client", {"type": "other"}):
            event = {"type": "session.start", "session": {"model": "m", "delegation": delegation}}
            with self.assertRaises(LiveError):
                policy.start_model(event)

    def test_start_needs_model_and_comes_first(self):
        for event in ({"type": "session.close"}, {"type": "session.start"},
                      {"type": "session.start", "session": {"model": ""}}):
            with self.assertRaises(LiveError):
                policy.start_model(event)

    def test_upstream_start_uses_the_deployment(self):
        frame = json.loads(policy.upstream_start(START, "gpt-live-1"))
        self.assertEqual("gpt-live-1", frame["session"]["model"])
        self.assertEqual("Deutsch", frame["session"]["instructions"])
        self.assertEqual("gpt-live", START["session"]["model"])

    def test_parse_rejects_binary_duplicates_and_large_frames(self):
        with self.assertRaises(LiveError) as ctx:
            policy.parse_event(None)
        self.assertEqual(1003, ctx.exception.code)
        with self.assertRaises(LiveError):
            policy.parse_event('{"type": "session.close", "type": "session.start"}')
        with self.assertRaises(LiveError) as ctx:
            policy.parse_event(json.dumps({"type": "x", "audio": "a" * policy.MAX_FRAME_BYTES}))
        self.assertEqual(1009, ctx.exception.code)
        for text in ("[]", "{}", "nope"):
            with self.assertRaises(LiveError):
                policy.parse_event(text)

    def test_client_events(self):
        for kind in policy.CLIENT_EVENTS:
            policy.check_client_event({"type": kind})
        for kind in ("session.start", "session.update", "response.create", "response.item.create"):
            with self.assertRaises(LiveError):
                policy.check_client_event({"type": kind})

    def test_live_url(self):
        expected = "wss://res.services.ai.azure.com/openai/v1/live/sessions"
        for base in ("https://res.services.ai.azure.com", "https://res.services.ai.azure.com/",
                     "https://res.services.ai.azure.com/openai", "https://res.services.ai.azure.com/openai/v1/"):
            self.assertEqual(expected, policy.live_url(base))
        with self.assertRaises(LiveError):
            policy.live_url("http://res")


class AccountingTests(unittest.TestCase):
    def test_rate_prefers_deployment_pricing(self):
        lookup = {"azure/gpt-live-1": {"input_cost_per_second": 0.0008}}.__getitem__
        self.assertEqual(0.002, accounting.resolve_rate({"input_cost_per_second": 0.002}, "gpt-live-1", lookup))
        self.assertEqual(0.003, accounting.resolve_rate(
            {"metadata": {"model_info": {"input_cost_per_second": "0.003"}}}, "gpt-live-1", lookup))
        self.assertEqual(0.0008, accounting.resolve_rate({}, "gpt-live-1", lookup))

    def test_rate_uses_base_model_for_other_deployment_names(self):
        prices = {"azure/gpt-live-1": {"input_cost_per_second": 0.0008}}

        def lookup(name):
            return prices[name]

        self.assertEqual(0.0008, accounting.resolve_rate(
            {"model_info": {"base_model": "azure/gpt-live-1"}}, "voice-prod", lookup))
        with self.assertRaises(LiveError):
            accounting.resolve_rate({}, "voice-prod", lookup)
        with self.assertRaises(LiveError):
            accounting.resolve_rate({"input_cost_per_second": 0}, "gpt-live-1", lookup)

    def test_seconds_from_session_closed(self):
        clock = Clock()
        acc = session(clock)
        acc.received({"type": "session.started", "session": {"id": "live_1"}})
        clock.now += 30
        acc.received({"type": "session.usage.updated", "usage": {"seconds": 14}})
        acc.received({"type": "session.usage.updated", "usage": {"seconds": 9}})
        acc.received({"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 21}})
        self.assertEqual((21, "provider_session_closed"), acc.billed_seconds())
        self.assertEqual("live_1", acc.summary()["provider_session_id"])

    def test_seconds_fall_back_to_elapsed_time(self):
        clock = Clock()
        acc = session(clock)
        self.assertEqual((0.0, "elapsed_since_session_started"), acc.billed_seconds())
        acc.received({"type": "session.started", "session": {}})
        clock.now += 75
        acc.received({"type": "session.usage.updated", "usage": {"seconds": 60}})
        self.assertEqual((75, "elapsed_since_session_started"), acc.billed_seconds())
        acc.received({"type": "session.usage.updated", "usage": {"seconds": 80}})
        self.assertEqual((80, "provider_usage_updated"), acc.billed_seconds())

    def test_finish_bills_once(self):
        clock = Clock()
        log = FakeLog()
        acc = session(clock, log)
        acc.received({"type": "session.started", "session": {"id": "s"}})
        acc.received({"type": "session.closed", "usage": {"seconds": 30}})
        for _ in range(2):
            asyncio.run(acc.finish(Result, "ok", "failed"))
        self.assertEqual(1, len(log.success))
        result = log.success[0]["result"]
        self.assertAlmostEqual(0.03, result._hidden_params["response_cost"])
        self.assertTrue(log.model_call_details["ok"])
        meta = log.model_call_details["litellm_params"]["metadata"]["spend_logs_metadata"]["gpt_live"]
        self.assertEqual(30, meta["billed_seconds"])
        self.assertEqual([], log.failure)

    def test_finish_logs_refused_session_as_failure(self):
        log = FakeLog()
        acc = session(log=log)
        acc.end_reason = "upstream_refused"
        asyncio.run(acc.finish(Result, "ok", "failed"))
        self.assertEqual([], log.success)
        self.assertEqual(1, len(log.failure))
        self.assertTrue(log.model_call_details["failed"])


class RelayTests(unittest.TestCase):
    def run_relay(self, client, upstream, max_seconds=5):
        acc = session()
        reason = asyncio.run(asyncio.wait_for(
            relay.relay(client, upstream, acc, ("geheimer-schluessel",), max_seconds), 10))
        return reason, acc

    def test_conversation_with_client_delegation(self):
        client = FakeClient()
        upstream = FakeUpstream([
            {"type": "session.started", "session": {"id": "live_1"}},
            {"type": "session.output_audio.delta", "delta": "AAAA"},
            {"type": "session.delegation.created", "delegation": {"id": "item_1", "target": "client"}},
            {"type": "error", "error": {"message": "echo geheimer-schluessel"}},
        ])
        client.text({"type": "session.input_audio.append", "audio": "AAAA"})
        client.text({"type": "session.commentary.append", "delegation_id": "item_1", "content": "19 Uhr"})
        client.text({"type": "session.close"})
        reason, acc = self.run_relay(client, upstream)
        self.assertEqual("client_close", reason)
        self.assertEqual(["session.input_audio.append", "session.commentary.append", "session.close"],
                         [e["type"] for e in upstream.sent])
        kinds = [e["type"] for e in client.sent]
        self.assertEqual(["session.started", "session.output_audio.delta", "session.delegation.created",
                          "error", "session.closed"], kinds)
        self.assertNotIn("geheimer-schluessel", json.dumps(client.sent))
        self.assertEqual((20.0, "provider_session_closed"), acc.billed_seconds())
        self.assertEqual(1, acc.delegations)

    def test_unsupported_event_is_answered_and_dropped(self):
        client = FakeClient()
        upstream = FakeUpstream([{"type": "session.started", "session": {}}])
        client.text({"type": "response.create"})
        client.text({"type": "session.close"})
        reason, _ = self.run_relay(client, upstream)
        self.assertEqual("client_close", reason)
        self.assertEqual(["session.close"], [e["type"] for e in upstream.sent])
        errors = [e for e in client.sent if e["type"] == "error"]
        self.assertIn("response.create", errors[0]["error"]["message"])

    def test_disconnect_closes_the_azure_session(self):
        client = FakeClient()
        upstream = FakeUpstream([{"type": "session.started", "session": {}}], closed_seconds=12)
        client.disconnect()
        reason, acc = self.run_relay(client, upstream)
        self.assertEqual("client_disconnect", reason)
        self.assertEqual(["session.close"], [e["type"] for e in upstream.sent])
        self.assertEqual((12, "provider_session_closed"), acc.billed_seconds())

    def test_duration_limit(self):
        client = FakeClient()
        upstream = FakeUpstream([{"type": "session.started", "session": {}}])
        reason, acc = self.run_relay(client, upstream, max_seconds=0.2)
        self.assertEqual("duration_limit", reason)
        self.assertEqual(["session.close"], [e["type"] for e in upstream.sent])
        self.assertTrue(acc.closed_by_provider)

    def test_azure_closing_on_its_own(self):
        client = FakeClient()
        upstream = FakeUpstream([
            {"type": "session.started", "session": {}},
            {"type": "session.closed", "reason": "expired", "usage": {"seconds": 900}},
        ])
        reason, acc = self.run_relay(client, upstream)
        self.assertEqual("provider_closed", reason)
        self.assertEqual("expired", acc.close_reason)

    def test_binary_client_frame_ends_the_session(self):
        client = FakeClient([{"type": "websocket.receive", "bytes": b"\x00"}])
        upstream = FakeUpstream([{"type": "session.started", "session": {}}])
        with self.assertRaises(LiveError) as ctx:
            self.run_relay(client, upstream)
        self.assertEqual(1003, ctx.exception.code)


class RunAzureTests(unittest.TestCase):
    def test_rewrites_model_and_bills(self):
        upstream = FakeUpstream([{"type": "session.started", "session": {"id": "s"}}], closed_seconds=10)
        seen = {}

        class Connect:
            def __init__(self, url, **kwargs):
                seen["url"] = url
                seen["headers"] = kwargs["additional_headers"]

            async def __aenter__(self):
                return upstream

            async def __aexit__(self, *exc):
                return False

        client = FakeClient()
        client.text({"type": "session.close"})
        log = FakeLog({"metadata": {}, "input_cost_per_second": 0.001})
        asyncio.run(relay.run_azure(
            client, model="gpt-live-1", api_base="https://res.services.ai.azure.com",
            auth_headers={"api-key": "k" * 20}, logging_obj=log,
            context={"alias": "gpt-live", "start": START}, connect=Connect))
        self.assertEqual("wss://res.services.ai.azure.com/openai/v1/live/sessions", seen["url"])
        self.assertEqual("gpt-live-1", upstream.sent[0]["session"]["model"])
        self.assertEqual("session.start", upstream.sent[0]["type"])
        self.assertEqual(1000, client.closed[0])
        self.assertAlmostEqual(0.01, log.success[0]["result"]._hidden_params["response_cost"])


class RouteTests(unittest.TestCase):
    def test_is_azure(self):
        from litellm.proxy.extensions.gpt_live.routes import is_azure

        self.assertTrue(is_azure({"litellm_params": {"model": "azure/gpt-live-1"}}))
        self.assertTrue(is_azure({"litellm_params": {"model": "gpt-live-1", "custom_llm_provider": "azure"}}))
        self.assertFalse(is_azure({"litellm_params": {"model": "openai/gpt-live-1"}}))
        self.assertFalse(is_azure({"litellm_params": {"model": "azure/x", "custom_llm_provider": "openai"}}))


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

    def test_azure_handler_hands_marked_sessions_to_the_relay(self):
        from litellm.llms.azure.realtime.handler import AzureOpenAIRealtime

        class Socket:
            scope = {policy.SCOPE_KEY: {"alias": "gpt-live", "start": START}}

        socket = Socket()
        with patch("litellm.proxy.extensions.gpt_live.relay.run_azure", new=AsyncMock()) as run:
            asyncio.run(AzureOpenAIRealtime().async_realtime(
                model="gpt-live-1", websocket=socket, logging_obj=FakeLog(),
                api_base="https://res.services.ai.azure.com", api_key="k" * 20))
        run.assert_awaited_once()
        self.assertIs(socket, run.call_args.args[0])
        self.assertEqual("gpt-live-1", run.call_args.kwargs["model"])
        self.assertEqual({"api-key": "k" * 20}, dict(run.call_args.kwargs["auth_headers"]))
        self.assertEqual("gpt-live", run.call_args.kwargs["context"]["alias"])

    def test_unmarked_sessions_keep_the_realtime_path(self):
        from litellm.llms.azure.realtime.handler import AzureOpenAIRealtime

        class Socket:
            scope = {}

        with patch("litellm.proxy.extensions.gpt_live.relay.run_azure", new=AsyncMock()) as run:
            with self.assertRaises(ValueError):
                asyncio.run(AzureOpenAIRealtime().async_realtime(
                    model="gpt-realtime", websocket=Socket(), logging_obj=FakeLog(), api_base=None))
        run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
