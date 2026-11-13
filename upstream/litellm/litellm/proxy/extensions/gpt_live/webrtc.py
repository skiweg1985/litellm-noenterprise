"""GPT-Live over WebRTC: the proxy sets up the session and holds its control channel.

Audio flows directly between the device and Azure. The proxy forwards the SDP
offer to Azure, returns the answer, and opens its own sideband connection
(/attach) to the new session. That connection reports usage, enforces the
duration limit and bills the session even if the client never attaches. The
client that created the session can attach to the same control channel through
the proxy.

Sessions live in the memory of the worker that created them. With several
proxy workers an attach can reach a worker that does not know the session.
"""

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import re
from urllib.parse import urlparse

from .accounting import SessionAccounting, resolve_rate
from .policy import (
    CLOSE_GRACE_SECONDS,
    LiveError,
    check_client_event,
    live_url,
    max_session_seconds,
    parse_event,
)

logger = logging.getLogger(__name__)

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
SAFETY_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
MAX_SDP_BYTES = 64 * 1024
MAX_ANSWER_BYTES = 128 * 1024
BACKLOG_EVENTS = 200
SIDEBAND_WAIT_SECONDS = 20
RETAIN_CLOSED_SECONDS = 300
CLOSE_FRAME = json.dumps({"type": "session.close"})

SESSIONS = {}


def owner_of(auth):
    """A stable fingerprint of the key that created a session."""
    token = getattr(auth, "token", None) or getattr(auth, "api_key", None) or ""
    return hashlib.sha256(str(token).encode()).hexdigest()


def sessions_url(api_base):
    """https://<resource>/openai/v1/live/sessions for the SDP exchange."""
    return "https://" + live_url(api_base)[len("wss://"):]


def parse_answer(status, headers, body):
    """Azure's answer to the SDP offer in the form clients expect."""
    if len(body) > MAX_ANSWER_BYTES:
        raise LiveError("Azure's WebRTC answer is too large.", 502)
    content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
    session_id = sdp = None
    if content_type == "application/json":
        try:
            data = json.loads(body)
        except ValueError:
            raise LiveError("Azure's WebRTC answer is not valid JSON.", 502) from None
        if isinstance(data, dict):
            session = data.get("session") if isinstance(data.get("session"), dict) else {}
            transport = data.get("transport") if isinstance(data.get("transport"), dict) else {}
            session_id = session.get("id") or data.get("id")
            sdp = transport.get("sdp") or data.get("sdp")
    elif content_type == "application/sdp":
        sdp = body.decode("utf-8", "replace")
        location = headers.get("location") or ""
        session_id = (
            headers.get("openai-session-id")
            or headers.get("x-session-id")
            or (urlparse(location).path.rstrip("/").rsplit("/", 1)[-1] if location else None)
        )
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        raise LiveError("Azure's WebRTC answer has no usable session ID.", 502)
    if not isinstance(sdp, str) or not sdp or len(sdp.encode()) > MAX_SDP_BYTES:
        raise LiveError("Azure's WebRTC answer has no usable SDP.", 502)
    return session_id, sdp


def upstream_error(status, body):
    """Azure's error for the client, without anything that could carry secrets."""
    message, code = f"Azure refused the GPT-Live session (HTTP {status}).", None
    try:
        error = json.loads(body).get("error") or {}
        if isinstance(error.get("message"), str):
            message = error["message"][:500]
        if isinstance(error.get("code"), str):
            code = error["code"][:64]
    except (ValueError, AttributeError):
        pass
    return {"error": {"type": "invalid_request_error" if status < 500 else "upstream_error",
                      "code": code, "message": message}}


class LiveSession:
    def __init__(self, session_id, owner, attach_url, headers, accounting, secrets=()):
        self.id = session_id
        self.owner = owner
        self.attach_url = attach_url
        self.headers = dict(headers)
        self.accounting = accounting
        self.secrets = list(secrets)
        self.backlog = collections.deque(maxlen=BACKLOG_EVENTS)
        self.upstream = None
        self.client = None
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.close_requested = False
        self.task = None

    def _redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text

    async def _deliver(self, raw):
        text = self._redact(raw)
        self.backlog.append(text)
        client = self.client
        if client is not None:
            try:
                await client.send_text(text)
            except Exception:  # noqa: BLE001 - a gone client does not end the session
                self.client = None

    async def _read(self, connect, ssl):
        async with connect(
            self.attach_url,
            additional_headers=self.headers,
            max_size=4 * 1024 * 1024,
            ssl=ssl,
            open_timeout=SIDEBAND_WAIT_SECONDS,
        ) as upstream:
            self.upstream = upstream
            self.ready.set()
            async for raw in upstream:
                if not isinstance(raw, str):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                self.accounting.received(event)
                await self._deliver(raw)
                if event.get("type") == "session.closed":
                    return "provider_closed"
        return "provider_disconnect"

    async def run(self, limit, connect, ssl=None):
        """Hold the sideband until Azure closes the session, then bill it once."""
        reader = asyncio.create_task(self._read(connect, ssl))
        try:
            done, _ = await asyncio.wait({reader}, timeout=limit)
            if done:
                reason = reader.result()
                closed_on_request = self.close_requested and reason == "provider_closed"
                self.accounting.end_reason = "client_close" if closed_on_request else reason
            else:
                self.accounting.end_reason = "duration_limit"
                with contextlib.suppress(Exception):
                    await self.upstream.send(CLOSE_FRAME)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(reader), CLOSE_GRACE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - provider errors may carry headers
            status = getattr(getattr(exc, "response", None), "status_code", None)
            self.accounting.end_reason = "sideband_refused" if status else "sideband_error"
            self.accounting.upstream_status = status
            logger.warning("GPT-Live sideband for %s ended: %s", self.id, type(exc).__name__)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            self.ready.set()
            self.finished.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self.accounting.finish())
            client, self.client = self.client, None
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close(code=1000, reason="GPT-Live session ended.")
            asyncio.get_running_loop().call_later(RETAIN_CLOSED_SECONDS, SESSIONS.pop, self.id, None)

    async def attach(self, client):
        """Forward the client's control events until it leaves or the session ends."""
        self.client = client
        try:
            for text in list(self.backlog):
                await client.send_text(text)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.ready.wait(), SIDEBAND_WAIT_SECONDS)
            finished = asyncio.ensure_future(self.finished.wait())
            try:
                while not self.finished.is_set():
                    receive = asyncio.ensure_future(client.receive())
                    await asyncio.wait({receive, finished}, return_when=asyncio.FIRST_COMPLETED)
                    if not receive.done():
                        receive.cancel()
                        return
                    message = receive.result()
                    if message["type"] == "websocket.disconnect":
                        return
                    await self._forward(message)
            finally:
                finished.cancel()
        finally:
            if self.client is client:
                self.client = None

    async def _forward(self, message):
        """One client event to Azure; rejected events are dropped, not answered."""
        try:
            event = check_client_event(parse_event(message.get("text")))
        except LiveError as exc:
            # An error event would end the conversation in the client.
            logger.warning("GPT-Live %s: dropped client event: %s", self.id, exc)
            return
        if self.upstream is None or self.finished.is_set():
            return
        with contextlib.suppress(Exception):
            await self.upstream.send(message["text"])
        if event["type"] == "session.close":
            self.close_requested = True


async def start(*, model, api_base, auth_headers, logging_obj, context, http_post=None, connect=None):
    """Called from relay.run_azure for a POST with transport webrtc.

    Writes {"result": ...} or {"error": (status, body)} into context. On success
    the session owns billing; the route must not release limits itself.
    """
    import websockets

    from litellm.llms.custom_httpx.http_handler import get_shared_realtime_ssl_context

    from .relay import _secrets

    ssl = get_shared_realtime_ssl_context()
    secrets = _secrets(auth_headers)
    accounting = None
    try:
        if not api_base:
            raise LiveError("The GPT-Live deployment has no api_base.", 500)
        rate = resolve_rate(logging_obj.model_call_details.get("litellm_params"), model)
        limit = max_session_seconds()
        accounting = SessionAccounting(logging_obj, context["alias"], model, rate)
        headers = {**auth_headers, "Content-Type": "application/json"}
        if context.get("safety_identifier"):
            headers["OpenAI-Safety-Identifier"] = context["safety_identifier"]
        body = {
            "session": {**context["session"], "model": model},
            "transport": {"type": "webrtc", "sdp": context["sdp"]},
        }
        status, response_headers, response_body = await (http_post or _http_post)(
            sessions_url(api_base), headers, body, ssl
        )
        if status >= 400:
            accounting.end_reason = "upstream_refused"
            accounting.upstream_status = status
            context["error"] = (status if status < 500 else 502, upstream_error(status, response_body))
            return
        session_id, sdp = parse_answer(status, response_headers, response_body)
        accounting.live_started = accounting.clock()
        accounting.provider_session_id = session_id
        live = LiveSession(
            session_id, context["owner"], live_url(api_base) + f"/{session_id}/attach",
            auth_headers, accounting, secrets,
        )
        SESSIONS[session_id] = live
        live.task = asyncio.create_task(live.run(limit, connect or websockets.connect, ssl))
        context["result"] = {"session": {"id": session_id}, "transport": {"type": "webrtc", "sdp": sdp}}
        context["owned"] = True
        accounting = None
    except LiveError as exc:
        if accounting is not None:
            accounting.end_reason = "rejected"
        status = exc.code if 400 <= exc.code < 600 else 500
        context["error"] = (status, {"error": {"type": "invalid_request_error", "message": str(exc)}})
    except Exception as exc:  # noqa: BLE001 - provider errors may carry headers
        if accounting is not None:
            accounting.end_reason = "upstream_error"
        logger.error("GPT-Live WebRTC setup failed: %s", type(exc).__name__)
        context["error"] = (502, {"error": {"type": "upstream_error", "message": "GPT-Live WebRTC setup failed."}})
    finally:
        if accounting is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(accounting.finish())


async def _http_post(url, headers, body, ssl):
    import httpx

    async with httpx.AsyncClient(verify=ssl, timeout=30) as client:
        response = await client.post(url, headers=headers, json=body)
        return response.status_code, response.headers, response.content
