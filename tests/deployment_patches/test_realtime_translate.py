"""Realtime Translate extension tests against the installed package; no network."""
import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.datastructures import QueryParams

from litellm.proxy.extensions.realtime_translate import accounting, policy, stream
from litellm.proxy.extensions.realtime_translate.policy import TranslationError
from test_gpt_live import FakeClient, FakeLog

AUDIO = base64.b64encode(b"\x01\x00" * 24000).decode()  # one second of PCM16 at 24 kHz
APPEND = {"type": "session.input_audio_buffer.append", "audio": AUDIO}
LANGUAGE = {"type": "session.update", "session": {"audio": {"output": {"language": "en"}}}}
DEPLOYMENT = {
    "model_name": "azure-live-translate",
    "litellm_params": {"model": "azure/gpt-realtime-translate", "api_base": "https://res.services.ai.azure.com",
                       "api_key": "k" * 20, "realtime_protocol": "GA"},
    "model_info": {"base_model": "azure/gpt-realtime-translate"},
}


class Upstream:
    """Azure's translation socket: yields queued events until closed."""

    def __init__(self, events=(), echo=None):
        self.queue = asyncio.Queue()
        for event in events:
            self.queue.put_nowait(json.dumps(event))
        self.sent = []
        self.echo = echo

    def push(self, event):
        self.queue.put_nowait(json.dumps(event))

    def end(self):
        self.queue.put_nowait(None)

    async def send(self, text):
        event = json.loads(text)
        self.sent.append(event)
        if self.echo and event["type"] == "session.input_audio_buffer.append":
            for reply in self.echo:
                self.push(reply)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class Connect:
    def __init__(self, upstream=None, error=None):
        self.upstream = upstream
        self.error = error
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        self.headers = kwargs["additional_headers"]
        return self

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.upstream

    async def __aexit__(self, *exc):
        return False


def run(client, upstream=None, error=None, log=None, rate=0.001):
    log = log or FakeLog({"metadata": {}, "input_cost_per_second": rate})
    connect = Connect(upstream, error)
    asyncio.run(asyncio.wait_for(stream.run_azure(
        client, model="gpt-realtime-translate", api_base="https://res.services.ai.azure.com",
        auth_headers={"api-key": "secret-key-123"}, logging_obj=log,
        context={"alias": "azure-live-translate"}, connect=connect,
    ), 10))
    return log, connect


class PolicyTests(unittest.TestCase):
    def test_requested_model(self):
        self.assertEqual("azure-live-translate", policy.requested_model(QueryParams("model=azure-live-translate")))
        for query in ("", "model=a&model=b", "model=a&api_base=x", "model=../x", "api_key=x"):
            with self.assertRaises(TranslationError):
                policy.requested_model(QueryParams(query))

    def test_client_events(self):
        self.assertEqual("session.input_audio_buffer.append", policy.client_event(json.dumps(APPEND))["type"])
        policy.client_event(json.dumps(LANGUAGE))
        policy.client_event(json.dumps({"type": "session.update", "session": {"audio": {"input": {
            "noise_reduction": {"type": "near_field"}, "transcription": {"model": "gpt-realtime-whisper"}}}}}))
        policy.client_event(json.dumps({"type": "session.update", "session": {"audio": {"input": {
            "noise_reduction": None, "transcription": None}}}}))
        for bad in (
            '{"type": "response.create"}',
            '{"type": "session.update", "session": {"model": "gpt-4o"}}',
            '{"type": "session.update", "session": {"audio": {"output": {"language": "en", "voice": "x"}}}}',
            '{"type": "session.update", "session": {"audio": {"output": {"language": "EN!"}}}}',
            '{"type": "session.input_audio_buffer.append", "audio": "AQ=="}',
            '{"type": "session.input_audio_buffer.append", "audio": "%%%"}',
            '{"type": "session.input_audio_buffer.append", "audio": "AQA=", "api_key": "x"}',
            '{"type": "session.update", "type": "session.update", "session": {"audio": {}}}',
            "[]",
            "not json",
            None,
            json.dumps({"type": "session.input_audio_buffer.append", "audio": "A" * policy.MAX_MESSAGE_BYTES}),
        ):
            with self.assertRaises(TranslationError, msg=str(bad)[:80]):
                policy.client_event(bad)

    def test_target(self):
        self.assertEqual(
            "wss://res.services.ai.azure.com/openai/v1/realtime/translations?model=gpt-realtime-translate",
            policy.translation_url("https://res.services.ai.azure.com/", "gpt-realtime-translate"),
        )
        for base in ("http://res", "https://res/openai", "https://u:p@res", "https://res?x=1", None):
            with self.assertRaises(TranslationError):
                policy.azure_origin(base)

    def test_translation_models(self):
        self.assertTrue(policy.is_translation_model("azure/gpt-realtime-translate"))
        self.assertTrue(policy.is_translation_model("gpt-realtime-translate-2026-05-01"))
        self.assertTrue(policy.is_translation_model("my-name", {"base_model": "azure/gpt-realtime-translate"}))
        self.assertTrue(policy.is_translation_model("my-name", {"realtime_translate": True}))
        self.assertFalse(policy.is_translation_model("gpt-realtime"))
        self.assertFalse(policy.is_translation_model("gpt-live-transcribe", {}))

    def test_deployment_check(self):
        policy.check_deployment(DEPLOYMENT)
        for change in (
            {"realtime_protocol": None},
            {"model": "azure/gpt-realtime"},
            {"model": "openai/gpt-realtime-translate"},
        ):
            bad = {**DEPLOYMENT, "model_info": {}, "litellm_params": {**DEPLOYMENT["litellm_params"], **change}}
            with self.assertRaises(TranslationError):
                policy.check_deployment(bad)

    def test_other_realtime_endpoints_refuse_translation_models(self):
        import litellm

        with self.assertRaises(litellm.BadRequestError):
            policy.reject_outside_route("gpt-realtime-translate")
        policy.reject_outside_route("gpt-realtime", {"base_model": "azure/gpt-realtime"})

    def test_session_limit(self):
        self.assertEqual(600, policy.max_session_seconds())
        for value, ok in (("30", True), ("0", False), ("3601", False), ("x", False)):
            with patch.dict(os.environ, {"LITELLM_TRANSLATE_MAX_SECONDS": value}):
                if ok:
                    self.assertEqual(int(value), policy.max_session_seconds())
                else:
                    with self.assertRaises(TranslationError):
                        policy.max_session_seconds()


class RateTests(unittest.TestCase):
    def test_order(self):
        def lookup(name):
            return {"azure/gpt-realtime-translate": {"input_cost_per_second": 0.0005}}[name]

        self.assertEqual(0.002, accounting.resolve_rate({"input_cost_per_second": 0.002}, "x", lookup))
        self.assertEqual(0.003, accounting.resolve_rate({"model_info": {"input_cost_per_second": 0.003}}, "x", lookup))
        self.assertEqual(0.0005, accounting.resolve_rate({"model_info": {"base_model": "azure/gpt-realtime-translate"}},
                                                         "custom-name", lookup))
        self.assertEqual(0.0005, accounting.resolve_rate({}, "gpt-realtime-translate", lookup))
        self.assertEqual(accounting.DEFAULT_INPUT_COST_PER_SECOND, accounting.resolve_rate({}, "custom-name", lookup))
        for bad in (0, -1, "x", float("nan")):
            with self.assertRaises(TranslationError):
                accounting.resolve_rate({"input_cost_per_second": bad}, "x", lookup)

    def test_price_list_knows_azure_translate(self):
        self.assertAlmostEqual(2.04 / 3600, accounting.resolve_rate({}, "gpt-realtime-translate"), places=9)


class RelayTests(unittest.TestCase):
    def test_translation_and_billing(self):
        upstream = Upstream([{"type": "session.created", "session": {"id": "sess_1"}}],
                            echo=[{"type": "session.output_transcript.delta", "delta": "Hello"},
                                  {"type": "session.output_audio.delta", "delta": AUDIO}])
        client = FakeClient()
        client.text(LANGUAGE)
        client.text(APPEND)
        client.text(APPEND)

        async def close_later():
            await asyncio.sleep(0.1)
            client.disconnect()

        log = FakeLog({"metadata": {}, "input_cost_per_second": 0.001})
        connect = Connect(upstream)

        async def main():
            asyncio.create_task(close_later())
            await stream.run_azure(
                client, model="gpt-realtime-translate", api_base="https://res.services.ai.azure.com",
                auth_headers={"api-key": "secret-key-123"}, logging_obj=log,
                context={"alias": "azure-live-translate"}, connect=connect,
            )

        asyncio.run(asyncio.wait_for(main(), 10))
        self.assertEqual(["wss://res.services.ai.azure.com/openai/v1/realtime/translations?model=gpt-realtime-translate"],
                         connect.urls)
        self.assertEqual({"api-key": "secret-key-123"}, connect.headers)
        self.assertEqual(["session.update", "session.input_audio_buffer.append", "session.input_audio_buffer.append"],
                         [e["type"] for e in upstream.sent])
        received = [e["type"] for e in client.sent]
        self.assertIn("session.output_transcript.delta", received)
        self.assertNotIn("error", received)
        self.assertEqual((1000, "Translation session ended."), client.closed)
        self.assertEqual(1, len(log.success))
        hidden = log.success[0]["result"]._hidden_params
        self.assertAlmostEqual(0.002, hidden["response_cost"])
        summary = hidden["realtime_translation"]
        self.assertEqual(("client_close", "sess_1", 2.0, 2.0),
                         (summary["end_reason"], summary["provider_session_id"],
                          summary["input_audio_seconds"], summary["output_audio_seconds"]))
        self.assertEqual(summary, log.model_call_details["litellm_params"]["metadata"]["spend_logs_metadata"]
                         ["realtime_translation"])

    def test_rejected_client_event_ends_session_and_bills_sent_audio(self):
        client = FakeClient()
        client.text(APPEND)
        client.text({"type": "response.create"})
        log, _ = run(client, Upstream())
        self.assertEqual(1008, client.closed[0])
        self.assertEqual("error", client.sent[-1]["type"])
        self.assertAlmostEqual(0.001, log.success[0]["result"]._hidden_params["response_cost"])
        self.assertEqual("rejected", log.success[0]["result"]._hidden_params["realtime_translation"]["end_reason"])

    def test_azure_error_is_not_forwarded(self):
        upstream = Upstream([{"type": "error", "error": {"code": "bad", "message": "header secret-key-123"}}])
        client = FakeClient()
        run(client, upstream)
        self.assertEqual(1011, client.closed[0])
        self.assertNotIn("secret-key-123", json.dumps(client.sent))
        self.assertEqual("Azure rejected a translation event.", client.sent[-1]["error"]["message"])

    def test_secrets_are_redacted(self):
        upstream = Upstream([{"type": "session.output_transcript.delta", "delta": "secret-key-123"}])
        upstream.end()
        client = FakeClient()
        log, _ = run(client, upstream)
        self.assertEqual("[redacted]", client.sent[0]["delta"])
        self.assertEqual("provider_closed", log.success[0]["result"]._hidden_params["realtime_translation"]["end_reason"])

    def test_duration_limit(self):
        client = FakeClient()
        with patch.dict(os.environ, {"LITELLM_TRANSLATE_MAX_SECONDS": "1"}):
            log, _ = run(client, Upstream())
        self.assertEqual((1000, "Translation duration limit reached."), client.closed)
        self.assertEqual("duration_limit", log.success[0]["result"]._hidden_params["realtime_translation"]["end_reason"])

    def test_refused_handshake_is_a_failure(self):
        import websockets

        refused = websockets.exceptions.InvalidStatus(SimpleNamespace(status_code=401, headers={}, body=b""))
        client = FakeClient()
        log, _ = run(client, error=refused)
        self.assertEqual(1011, client.closed[0])
        self.assertIn("HTTP 401", client.sent[-1]["error"]["message"])
        self.assertEqual([], log.success)
        self.assertEqual(1, len(log.failure))

    def test_unusable_api_base_is_a_failure(self):
        client = FakeClient()
        log = FakeLog({"metadata": {}, "input_cost_per_second": 0.001})
        asyncio.run(stream.run_azure(
            client, model="gpt-realtime-translate", api_base="https://res/openai/v1", auth_headers={},
            logging_obj=log, context={"alias": "a"}, connect=Connect(Upstream()),
        ))
        self.assertEqual(1011, client.closed[0])
        self.assertEqual([], log.success)


class IntegrationTests(unittest.TestCase):
    def test_route_is_registered_and_an_llm_api_route(self):
        from litellm.proxy.auth.route_checks import RouteChecks
        from litellm.proxy.proxy_server import app
        from starlette.routing import WebSocketRoute

        paths = [r.path for r in app.routes if isinstance(r, WebSocketRoute)]
        self.assertEqual(1, paths.count("/v1/realtime/translations"))
        self.assertTrue(RouteChecks.is_llm_api_route("/v1/realtime/translations"))

    def test_azure_handler_hands_marked_sessions_to_the_relay(self):
        from litellm.llms.azure.realtime.handler import AzureOpenAIRealtime

        calls = []

        async def fake_run(websocket, **kwargs):
            calls.append(kwargs)

        websocket = SimpleNamespace(scope={"litellm.realtime_translate": {"alias": "azure-live-translate"}})
        with patch("litellm.proxy.extensions.realtime_translate.stream.run_azure", fake_run):
            asyncio.run(AzureOpenAIRealtime().async_realtime(
                model="gpt-realtime-translate", websocket=websocket, logging_obj=FakeLog(),
                api_base="https://res.services.ai.azure.com", api_key="k" * 20, realtime_protocol="GA",
            ))
        self.assertEqual("gpt-realtime-translate", calls[0]["model"])
        self.assertEqual({"alias": "azure-live-translate"}, calls[0]["context"])

    def test_plain_realtime_refuses_translation_deployments(self):
        import litellm
        from litellm.llms.azure.realtime.handler import AzureOpenAIRealtime

        with self.assertRaises(litellm.BadRequestError):
            asyncio.run(AzureOpenAIRealtime().async_realtime(
                model="gpt-realtime-translate", websocket=SimpleNamespace(scope={}), logging_obj=FakeLog(),
                api_base="https://res.services.ai.azure.com", api_key="k" * 20, realtime_protocol="GA",
            ))


if __name__ == "__main__":
    unittest.main()
