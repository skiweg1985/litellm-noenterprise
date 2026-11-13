"""Relay between the client and Azure GPT-Live, called from the Azure realtime handler.

LiteLLM's router has picked the deployment and resolved its credentials when
this runs. The relay opens /openai/v1/live/sessions, sends the client's start
frame with the deployment name as model, and forwards events in both
directions: client events only from the allowlist, Azure events unchanged.
"""

import asyncio
import contextlib
import json
import logging

from .accounting import SessionAccounting, resolve_rate
from .policy import (
    CLOSE_GRACE_SECONDS,
    LiveError,
    check_client_event,
    error_frame,
    live_url,
    max_session_seconds,
    parse_event,
    upstream_start,
)

logger = logging.getLogger(__name__)

CLOSE_FRAME = json.dumps({"type": "session.close"})


def _redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _secrets(auth_headers):
    values = []
    for value in auth_headers.values():
        values.append(value)
        if value.lower().startswith("bearer "):
            values.append(value[7:])
    return [v for v in values if len(v) >= 8]


async def close_client(client, code=1000, reason="GPT-Live session ended.", notify=False):
    if notify:
        with contextlib.suppress(Exception):
            await client.send_text(error_frame(reason, "gpt_live_error"))
    with contextlib.suppress(Exception):
        await client.close(code=code, reason=reason[:120])


async def relay(client, upstream, accounting, secrets=(), max_seconds=None):
    """Forward until either side ends and return why the session ended:
    client_close, client_disconnect, duration_limit, provider_closed or
    provider_disconnect."""
    client_gone = asyncio.Event()
    close_requested = False

    async def client_to_upstream():
        nonlocal close_requested
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                client_gone.set()
                return
            event = parse_event(message.get("text"))
            try:
                check_client_event(event)
            except LiveError as exc:
                # One unsupported event does not end a conversation.
                await client.send_text(error_frame(str(exc)))
                continue
            await upstream.send(message["text"])
            if event["type"] == "session.close":
                close_requested = True

    async def upstream_to_client():
        async for raw in upstream:
            if not isinstance(raw, str):
                raise LiveError("Unexpected binary frame from Azure.", 1011)
            try:
                event = json.loads(raw)
            except ValueError:
                raise LiveError("Invalid event from Azure.", 1011) from None
            if not isinstance(event, dict):
                raise LiveError("Invalid event from Azure.", 1011)
            accounting.received(event)
            if not client_gone.is_set():
                with contextlib.suppress(Exception):
                    await client.send_text(_redact(raw, secrets))
            if event.get("type") == "session.closed":
                return "provider_closed"
        return "provider_disconnect"

    sender = asyncio.create_task(client_to_upstream())
    receiver = asyncio.create_task(upstream_to_client())
    try:
        done, _ = await asyncio.wait(
            {sender, receiver}, timeout=max_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if receiver in done:
            reason = receiver.result()
            return "client_close" if close_requested else reason
        if sender in done:
            sender.result()  # raises LiveError on a protocol violation of the client
            reason = "client_disconnect"
        else:
            reason = "duration_limit"
        # Ask Azure to end the session so session.closed reports the final usage.
        sender.cancel()
        with contextlib.suppress(Exception):
            await upstream.send(CLOSE_FRAME)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(receiver), CLOSE_GRACE_SECONDS)
        return reason
    finally:
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)


async def run_azure(websocket, *, model, api_base, auth_headers, logging_obj, context, connect=None):
    """Entry point from AzureOpenAIRealtime.async_realtime."""
    if context.get("transport") == "webrtc":
        from .webrtc import start

        return await start(
            model=model, api_base=api_base, auth_headers=auth_headers,
            logging_obj=logging_obj, context=context,
        )
    import websockets
    from websockets.exceptions import InvalidStatus

    from litellm.llms.custom_httpx.http_handler import get_shared_realtime_ssl_context

    accounting = None
    secrets = _secrets(auth_headers)
    try:
        if not api_base:
            raise LiveError("The GPT-Live deployment has no api_base.", 1011)
        url = live_url(api_base)
        rate = resolve_rate(logging_obj.model_call_details.get("litellm_params"), model)
        limit = max_session_seconds()
        accounting = SessionAccounting(logging_obj, context["alias"], model, rate)
        opener = connect or websockets.connect
        async with opener(
            url,
            additional_headers=dict(auth_headers),
            max_size=4 * 1024 * 1024,
            ssl=get_shared_realtime_ssl_context(),
            open_timeout=20,
        ) as upstream:
            await upstream.send(upstream_start(context["start"], model))
            accounting.end_reason = await relay(websocket, upstream, accounting, secrets, limit)
        message = {
            "duration_limit": "GPT-Live duration limit reached.",
            "provider_disconnect": "Azure ended the GPT-Live connection.",
        }.get(accounting.end_reason, "GPT-Live session ended.")
        await close_client(websocket, 1000, message, notify=accounting.end_reason == "provider_disconnect")
    except InvalidStatus as exc:
        status = exc.response.status_code
        if accounting is not None:
            accounting.end_reason = "upstream_refused"
            accounting.upstream_status = status
        logger.warning("Azure refused the GPT-Live handshake with HTTP %s", status)
        await close_client(websocket, 1011, f"Azure refused the GPT-Live session (HTTP {status}).", notify=True)
    except LiveError as exc:
        if accounting is not None:
            accounting.end_reason = "rejected"
        await close_client(websocket, exc.code, str(exc), notify=True)
    except asyncio.CancelledError:
        if accounting is not None:
            accounting.end_reason = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - provider errors may carry headers
        if accounting is not None:
            accounting.end_reason = "relay_error"
        logger.error("GPT-Live relay failed: %s", _redact(type(exc).__name__ + ": " + str(exc), secrets)[:300])
        await close_client(websocket, 1011, "GPT-Live relay failed.", notify=True)
    finally:
        if accounting is not None:
            # Billing must survive a cancelled task.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(accounting.finish())
