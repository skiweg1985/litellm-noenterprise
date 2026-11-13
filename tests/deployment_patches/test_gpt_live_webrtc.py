"""GPT-Live WebRTC setup and control channel against the installed package; no network."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from litellm.proxy.extensions.gpt_live import policy, routes, webrtc
from litellm.proxy.extensions.gpt_live.policy import LiveError
from test_gpt_live import FakeClient, FakeLog, FakeUpstream

SDP_OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
SDP_ANSWER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=answer\r\n"


def json_answer(session_id="live_abc123", sdp=SDP_ANSWER, status=201):
    body = json.dumps({"session": {"id": session_id}, "transport": {"type": "webrtc", "sdp": sdp}}).encode()
    return status, {"content-type": "application/json"}, body


class Post:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def __call__(self, url, headers, body, ssl):
        self.calls.append({"url": url, "headers": headers, "body": body})
        return self.answer


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


def context(**extra):
    return {
        "transport": "webrtc",
        "alias": "gpt-live",
        "session": {"model": "gpt-live", "delegation": {"type": "client"}, "store": False},
        "sdp": SDP_OFFER,
        "owner": "owner-a",
        "safety_identifier": "a" * 64,
        **extra,
    }


async def start(ctx, post, connect, log=None):
    log = log or FakeLog({"metadata": {}, "input_cost_per_second": 0.001})
    await webrtc.start(
        model="gpt-live-1", api_base="https://res.services.ai.azure.com", auth_headers={"api-key": "k" * 20},
        logging_obj=log, context=ctx, http_post=post, connect=connect,
    )
    return log


class AnswerTests(unittest.TestCase):
    def test_json_answer(self):
        self.assertEqual(("live_abc123", SDP_ANSWER), webrtc.parse_answer(*json_answer()))

    def test_sdp_answer_with_location(self):
        headers = {"content-type": "application/sdp", "location": "/openai/v1/live/sessions/live_xyz"}
        self.assertEqual(("live_xyz", SDP_ANSWER), webrtc.parse_answer(201, headers, SDP_ANSWER.encode()))

    def test_rejects_unusable_answers(self):
        for answer in (
            json_answer(session_id="bad id!"),
            json_answer(session_id="x" * 201),
            json_answer(sdp=""),
            json_answer(sdp="v" * (webrtc.MAX_SDP_BYTES + 1)),
            (201, {"content-type": "text/plain"}, b"nope"),
            (201, {"content-type": "application/json"}, b"{"),
        ):
            with self.assertRaises(LiveError):
                webrtc.parse_answer(*answer)

    def test_upstream_error_keeps_message_only(self):
        body = json.dumps({"error": {"message": "Offer did not have an audio media section.",
                                     "code": "invalid_offer", "secret": "x"}}).encode()
        error = webrtc.upstream_error(400, body)["error"]
        self.assertEqual("invalid_offer", error["code"])
        self.assertNotIn("secret", error)
        self.assertIn("HTTP 502", webrtc.upstream_error(502, b"<html>")["error"]["message"])

    def test_request_body(self):
        body = json.dumps({"session": {"model": "gpt-live"}, "transport": {"type": "webrtc", "sdp": SDP_OFFER}})
        self.assertEqual("gpt-live", routes.read_webrtc_request(body.encode())[0])
        for bad in (
            {"session": {"model": "gpt-live"}, "transport": {"type": "websocket"}},
            {"session": {"model": "gpt-live"}, "transport": {"type": "webrtc", "sdp": ""}},
            {"session": {"model": "gpt-live", "delegation": {"type": "responses"}},
             "transport": {"type": "webrtc", "sdp": SDP_OFFER}},
            {"transport": {"type": "webrtc", "sdp": SDP_OFFER}},
        ):
            with self.assertRaises(LiveError):
                routes.read_webrtc_request(json.dumps(bad).encode())
        with self.assertRaises(LiveError):
            routes.read_webrtc_request(b"x" * (routes.MAX_BODY_BYTES + 1))


class SessionTests(unittest.TestCase):
    def setUp(self):
        webrtc.SESSIONS.clear()
        self.addCleanup(webrtc.SESSIONS.clear)

    def test_setup_attach_and_billing(self):
        async def scenario():
            upstream = FakeUpstream([{"type": "session.started", "session": {"id": "live_abc123"}}], closed_seconds=42)
            connect, post, ctx = Connect(upstream), Post(json_answer()), context()
            log = await start(ctx, post, connect)
            self.assertTrue(ctx["owned"])
            self.assertEqual({"session": {"id": "live_abc123"}, "transport": {"type": "webrtc", "sdp": SDP_ANSWER}},
                             ctx["result"])
            sent = post.calls[0]
            self.assertEqual("https://res.services.ai.azure.com/openai/v1/live/sessions", sent["url"])
            self.assertEqual("gpt-live-1", sent["body"]["session"]["model"])
            self.assertEqual(SDP_OFFER, sent["body"]["transport"]["sdp"])
            self.assertEqual("a" * 64, sent["headers"]["OpenAI-Safety-Identifier"])
            live = webrtc.SESSIONS["live_abc123"]
            client = FakeClient()
            client.text({"type": "session.commentary.append", "event_id": "e1", "delegation_id": None,
                         "content": "Hallo"})
            client.text({"type": "response.create"})
            client.text({"type": "session.close"})
            await asyncio.wait_for(live.attach(client), 5)
            await asyncio.wait_for(live.task, 5)
            self.assertEqual(["wss://res.services.ai.azure.com/openai/v1/live/sessions/live_abc123/attach"],
                             connect.urls)
            self.assertEqual(["session.commentary.append", "session.close"], [e["type"] for e in upstream.sent])
            self.assertIsNone(upstream.sent[0]["delegation_id"])
            self.assertNotIn("error", [e["type"] for e in client.sent])
            self.assertIn("session.closed", [e["type"] for e in client.sent])
            self.assertEqual(1, len(log.success))
            self.assertAlmostEqual(0.042, log.success[0]["result"]._hidden_params["response_cost"])
            self.assertEqual("client_close", live.accounting.end_reason)

        asyncio.run(scenario())

    def test_session_without_attach_is_billed(self):
        async def scenario():
            upstream = FakeUpstream([{"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 9}}])
            ctx = context()
            log = await start(ctx, Post(json_answer()), Connect(upstream))
            await asyncio.wait_for(webrtc.SESSIONS["live_abc123"].task, 5)
            self.assertAlmostEqual(0.009, log.success[0]["result"]._hidden_params["response_cost"])

        asyncio.run(scenario())

    def test_duration_limit_closes_through_the_sideband(self):
        async def scenario():
            upstream = FakeUpstream([], closed_seconds=3)
            with patch.dict(os.environ, {"GPT_LIVE_MAX_SESSION_SECONDS": "1"}):
                log = await start(context(), Post(json_answer()), Connect(upstream))
            live = webrtc.SESSIONS["live_abc123"]
            await asyncio.wait_for(live.task, 10)
            self.assertEqual(["session.close"], [e["type"] for e in upstream.sent])
            self.assertEqual("duration_limit", live.accounting.end_reason)
            self.assertEqual(1, len(log.success))

        asyncio.run(scenario())

    def test_refused_sideband_bills_elapsed_time(self):
        async def scenario():
            refused = RuntimeError("refused")
            refused.response = SimpleNamespace(status_code=404)
            log = await start(context(), Post(json_answer()), Connect(error=refused))
            live = webrtc.SESSIONS["live_abc123"]
            await asyncio.wait_for(live.task, 5)
            self.assertEqual("sideband_refused", live.accounting.end_reason)
            self.assertEqual(404, live.accounting.upstream_status)
            self.assertEqual(1, len(log.success))

        asyncio.run(scenario())

    def test_azure_refusal_is_returned_and_logged_as_failure(self):
        async def scenario():
            body = json.dumps({"error": {"message": "Offer did not have an audio media section.",
                                         "code": "invalid_offer"}}).encode()
            ctx = context()
            log = await start(ctx, Post((400, {"content-type": "application/json"}, body)), Connect())
            status, payload = ctx["error"]
            self.assertEqual(400, status)
            self.assertEqual("invalid_offer", payload["error"]["code"])
            self.assertNotIn("owned", ctx)
            self.assertEqual({}, webrtc.SESSIONS)
            self.assertEqual(1, len(log.failure))
            self.assertEqual([], log.success)

        asyncio.run(scenario())


class Socket:
    def __init__(self):
        self.denied = None
        self.accepted = False
        self.headers = {}

    async def send_denial_response(self, response):
        self.denied = response.status_code

    async def accept(self, **kwargs):
        self.accepted = True


class AttachRouteTests(unittest.TestCase):
    def setUp(self):
        webrtc.SESSIONS.clear()
        self.addCleanup(webrtc.SESSIONS.clear)

    def run_attach(self, session_id, token):
        socket = Socket()
        asyncio.run(routes.attach(socket, session_id, SimpleNamespace(token=token, api_key=None)))
        return socket

    def test_unknown_and_foreign_sessions_are_denied(self):
        self.assertEqual(404, self.run_attach("live_missing", "key-a").denied)

        async def register():
            live = webrtc.LiveSession("live_1", webrtc.owner_of(SimpleNamespace(token="key-a")), "wss://x", {}, None)
            webrtc.SESSIONS["live_1"] = live

        asyncio.run(register())
        socket = self.run_attach("live_1", "key-b")
        self.assertEqual(403, socket.denied)
        self.assertFalse(socket.accepted)

    def test_routes_are_llm_api_routes(self):
        from litellm.proxy.auth.route_checks import RouteChecks

        self.assertTrue(RouteChecks.is_llm_api_route("/v1/live/sessions/live_abc123/attach"))
        self.assertTrue(RouteChecks.is_llm_api_route("/live/sessions/live_abc123/attach"))

    def test_proxy_registers_post_and_attach(self):
        from litellm.proxy.proxy_server import app

        found = {(getattr(r, "path", None), tuple(sorted(getattr(r, "methods", None) or ["WS"]))) for r in app.routes}
        for path in policy.PATHS:
            self.assertIn((path, ("POST",)), found)
            self.assertIn((path, ("WS",)), found)
        for path in policy.ATTACH_PATHS:
            self.assertIn((path, ("WS",)), found)


if __name__ == "__main__":
    unittest.main()
