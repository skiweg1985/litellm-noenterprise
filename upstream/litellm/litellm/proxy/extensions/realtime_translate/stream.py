"""Relay between the client and Azure Realtime Translate, called from the Azure realtime handler.

LiteLLM's router has picked the deployment and resolved its credentials when
this runs. The relay opens /openai/v1/realtime/translations on the deployment's
resource and forwards events without the turn handling of ordinary realtime
sessions: client events only from the allowlist, Azure events unchanged.
"""

import asyncio
import contextlib
import json
import logging

from .accounting import SessionAccounting, resolve_rate
from .policy import (
    MAX_MESSAGE_BYTES,
    TranslationError,
    client_event,
    error_frame,
    max_session_seconds,
    translation_url,
)

logger = logging.getLogger(__name__)

CLOSE_MESSAGES = {
    "duration_limit": "Translation duration limit reached.",
    "provider_closed": "Azure ended the translation session.",
}


def _secrets(auth_headers):
    values = []
    for value in auth_headers.values():
        values.append(value)
        if value.lower().startswith("bearer "):
            values.append(value[7:])
    return [v for v in values if len(v) >= 8]


def _redact(text, secrets):
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text


async def close_client(client, code=1000, reason="Translation session ended."):
    if code != 1000:
        with contextlib.suppress(Exception):
            await client.send_text(error_frame(reason))
    with contextlib.suppress(Exception):
        await client.close(code=code, reason=reason[:120])


async def relay(client, upstream, accounting, secrets=(), max_seconds=None):
    """Forward until either side ends and return why the session ended:
    client_close, client_disconnect, duration_limit or provider_closed."""

    async def client_to_upstream():
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                return "client_close" if message.get("code", 1000) in (1000, 1001) else "client_disconnect"
            event = client_event(message.get("text"))
            await upstream.send(message["text"])
            accounting.sent(event)

    async def upstream_to_client():
        async for raw in upstream:
            if not isinstance(raw, str):
                raise TranslationError("Unexpected binary frame from Azure.", 1011)
            try:
                event = json.loads(raw)
            except (ValueError, RecursionError):
                raise TranslationError("Invalid event from Azure.", 1011) from None
            if not isinstance(event, dict):
                raise TranslationError("Invalid event from Azure.", 1011)
            if event.get("type") == "error":
                # Azure's error payloads can echo input and headers; keep only the code.
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                logger.warning("Azure rejected a translation event: %s", str(error.get("code"))[:64])
                raise TranslationError("Azure rejected a translation event.", 1011)
            accounting.received(event)
            with contextlib.suppress(Exception):
                await client.send_text(_redact(raw, secrets))
        return "provider_closed"

    sender = asyncio.create_task(client_to_upstream())
    receiver = asyncio.create_task(upstream_to_client())
    try:
        done, _ = await asyncio.wait({sender, receiver}, timeout=max_seconds, return_when=asyncio.FIRST_COMPLETED)
        if sender in done:
            return sender.result()
        if receiver in done:
            return receiver.result()
        return "duration_limit"
    finally:
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)


async def run_azure(websocket, *, model, api_base, auth_headers, logging_obj, context, connect=None):
    """Entry point from AzureOpenAIRealtime.async_realtime."""
    import websockets
    from websockets.exceptions import InvalidStatus

    from litellm.llms.custom_httpx.http_handler import get_shared_realtime_ssl_context

    accounting = None
    secrets = _secrets(auth_headers)
    try:
        url = translation_url(api_base, model)
        rate = resolve_rate(logging_obj.model_call_details.get("litellm_params"), model)
        limit = max_session_seconds()
        accounting = SessionAccounting(logging_obj, context["alias"], model, rate)
        opener = connect or websockets.connect
        async with opener(
            url,
            additional_headers=dict(auth_headers),
            max_size=MAX_MESSAGE_BYTES,
            ssl=get_shared_realtime_ssl_context(),
            open_timeout=15,
            close_timeout=2,
        ) as upstream:
            accounting.connected = True
            accounting.end_reason = await relay(websocket, upstream, accounting, secrets, limit)
        await close_client(websocket, 1000, CLOSE_MESSAGES.get(accounting.end_reason, "Translation session ended."))
    except InvalidStatus as exc:
        status = exc.response.status_code
        if accounting is not None:
            accounting.end_reason = "upstream_refused"
            accounting.upstream_status = status
        logger.warning("Azure refused the translation handshake with HTTP %s", status)
        await close_client(websocket, 1011, f"Azure refused the translation session (HTTP {status}).")
    except TranslationError as exc:
        if accounting is not None:
            accounting.end_reason = "rejected"
        await close_client(websocket, exc.code, str(exc))
    except asyncio.CancelledError:
        if accounting is not None:
            accounting.end_reason = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - provider errors may carry headers
        if accounting is not None:
            accounting.end_reason = "upstream_error"
        logger.error("Translation relay failed: %s", _redact(type(exc).__name__ + ": " + str(exc), secrets)[:300])
        await close_client(websocket, 1011, "Azure translation connection failed.")
    finally:
        if accounting is not None:
            # Billing must survive a cancelled task.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(accounting.finish())
