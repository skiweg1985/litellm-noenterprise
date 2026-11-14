"""Relay between the client and a Bedrock Nova Sonic stream.

Called from BedrockRealtime.async_realtime once LiteLLM's router has picked the
deployment and the handler has resolved its AWS credentials. The relay opens
InvokeModelWithBidirectionalStream and forwards events in both directions:
client events only from the allowlist, Bedrock events unchanged.
"""

import asyncio
import contextlib
import json
import logging

from .accounting import SessionAccounting, resolve_prices
from .policy import (
    CLOSE_GRACE_SECONDS,
    SonicError,
    check_client_event,
    encode,
    error_frame,
    parse_frame,
)

logger = logging.getLogger(__name__)

END_MESSAGES = {
    "client_close": "Nova Sonic session ended.",
    "client_disconnect": "Nova Sonic session ended.",
    "provider_closed": "Bedrock ended the Nova Sonic stream.",
}


def _redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


class StreamState:
    """What the client has opened, so the proxy can close it in Bedrock's order:
    open content, then the prompt, then the session."""

    def __init__(self):
        self.session_started = False
        self.prompt = None
        self.open_content = []
        self.prompt_ended = False
        self.session_ended = False

    def sent(self, name, body):
        if name == "sessionStart":
            self.session_started = True
        elif name == "promptStart":
            self.prompt = body.get("promptName")
        elif name == "contentStart":
            self.open_content.append(body.get("contentName"))
        elif name == "contentEnd":
            with contextlib.suppress(ValueError):
                self.open_content.remove(body.get("contentName"))
        elif name == "promptEnd":
            self.prompt_ended = True
        elif name == "sessionEnd":
            self.session_ended = True

    def closing_events(self):
        if not self.session_started or self.session_ended:
            return []
        events = []
        if self.prompt is not None and not self.prompt_ended:
            events += [
                encode("contentEnd", {"promptName": self.prompt, "contentName": name})
                for name in self.open_content
            ]
            events.append(encode("promptEnd", {"promptName": self.prompt}))
        events.append(encode("sessionEnd", {}))
        return events


def default_chunk(payload):
    from aws_sdk_bedrock_runtime.models import (
        BidirectionalInputPayloadPart,
        InvokeModelWithBidirectionalStreamInputChunk,
    )

    return InvokeModelWithBidirectionalStreamInputChunk(value=BidirectionalInputPayloadPart(bytes_=payload))


async def close_client(client, code=1000, reason="Nova Sonic session ended.", error=None):
    if error is not None:
        with contextlib.suppress(Exception):
            await client.send_text(error)
    with contextlib.suppress(Exception):
        await client.close(code=code, reason=reason[:120])


async def relay(client, stream, accounting, chunk=default_chunk):
    """Forward until either side ends and return why the stream ended:
    client_close, client_disconnect or provider_closed. Bedrock errors and
    fatal client errors propagate."""
    state = StreamState()
    client_gone = asyncio.Event()

    async def send(text):
        await stream.input_stream.send(chunk(text.encode()))

    async def client_to_bedrock():
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                client_gone.set()
                return "client_disconnect"
            name, body = parse_frame(message.get("text"))
            try:
                check_client_event(name, body)
            except SonicError as exc:
                # One unsupported event does not end a conversation.
                await client.send_text(error_frame(str(exc)))
                continue
            await send(message["text"])
            state.sent(name, body)
            if name == "sessionEnd":
                return "client_close"

    async def bedrock_to_client():
        output = (await stream.await_output())[1]
        accounting.accepted = True
        while True:
            chunk_ = await output.receive()
            if chunk_ is None:
                return "provider_closed"
            payload = chunk_.value.bytes_ if chunk_.value else None
            if not payload:
                continue
            text = payload.decode("utf-8")
            with contextlib.suppress(ValueError, AttributeError):
                event = json.loads(text).get("event")
                if isinstance(event, dict) and len(event) == 1:
                    ((name, body),) = event.items()
                    if isinstance(body, dict):
                        accounting.received(name, body)
            if not client_gone.is_set():
                with contextlib.suppress(Exception):
                    await client.send_text(text)

    sender = asyncio.create_task(client_to_bedrock())
    receiver = asyncio.create_task(bedrock_to_client())
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        if receiver.done():
            reason = receiver.result()  # raises the Bedrock error
            return "client_close" if state.session_ended else reason
        sender.cancel()
        client_error = sender.exception() if not sender.cancelled() else None
        # Close what the client left open, so that Bedrock reports final usage.
        with contextlib.suppress(Exception):
            for text in state.closing_events():
                await send(text)
            await stream.input_stream.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(receiver), CLOSE_GRACE_SECONDS)
        if client_error is not None:
            raise client_error
        return sender.result()
    finally:
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)


async def run_bedrock(websocket, *, model, open_stream, logging_obj, context, secrets=(), chunk=default_chunk):
    """Entry point from BedrockRealtime.async_realtime."""
    accounting = None
    try:
        prices = resolve_prices(logging_obj.model_call_details.get("litellm_params"), model)
        accounting = SessionAccounting(logging_obj, context["alias"], model, prices)
        stream = await open_stream()
        try:
            accounting.end_reason = await relay(websocket, stream, accounting, chunk)
        finally:
            with contextlib.suppress(Exception):
                await stream.input_stream.close()
        await close_client(websocket, 1000, END_MESSAGES.get(accounting.end_reason, "Nova Sonic session ended."))
    except SonicError as exc:
        if accounting is not None:
            accounting.end_reason = "rejected"
        await close_client(websocket, exc.code, str(exc), error_frame(str(exc)))
    except asyncio.CancelledError:
        if accounting is not None:
            accounting.end_reason = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - Bedrock errors keep their type
        kind = type(exc).__name__
        message = _redact(str(exc), secrets)[:300]
        if accounting is not None:
            accounting.end_reason = "provider_error"
            accounting.provider_error = kind
        logger.warning("Nova Sonic stream ended with %s: %s", kind, message)
        await close_client(
            websocket, 1011, f"Bedrock ended the Nova Sonic stream: {kind}.", error_frame(message, kind)
        )
    finally:
        if accounting is not None:
            # Billing must survive a cancelled task.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(accounting.finish())
