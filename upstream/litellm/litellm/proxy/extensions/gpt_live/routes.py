"""WebSocket route /v1/live/sessions with LiteLLM's own key, budget and limit checks.

The route follows LiteLLM's /v1/realtime endpoint. The difference: GPT-Live
names the model in the first frame (session.start), not in the URL. The route
therefore accepts the connection, reads that frame, checks the model against
the key and only then runs the pre-call checks and routes the request.
"""

import asyncio
import contextlib
import json
import logging

from fastapi import Depends, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from . import webrtc
from .policy import (
    ATTACH_PATHS,
    PATHS,
    SCOPE_KEY,
    START_TIMEOUT_SECONDS,
    LiveError,
    error_frame,
    parse_event,
    start_model,
)

logger = logging.getLogger(__name__)

# Request fields that would bypass the router's choice of deployment.
OVERRIDES = ("api_base", "base_url", "api_key", "azure_ad_token", "api_version")


def is_azure(deployment):
    params = deployment.get("litellm_params") or {}
    provider = params.get("custom_llm_provider")
    return provider == "azure" or (provider is None and str(params.get("model", "")).startswith("azure/"))


async def read_start(websocket):
    try:
        message = await asyncio.wait_for(websocket.receive(), START_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise LiveError("session.start was not sent in time.") from None
    if message["type"] == "websocket.disconnect":
        return None
    event = parse_event(message.get("text"))
    return event, start_model(event)


async def reject(websocket, proxy, auth, code, message):
    try:
        with contextlib.suppress(Exception):
            await websocket.send_text(error_frame(message))
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=message[:120])
    finally:
        await proxy._release_realtime_budget_reservation(auth)
        await proxy._release_realtime_max_parallel_slot(auth)


async def handle(websocket, auth, proxy):
    from litellm.constants import (
        REALTIME_SESSION_FAILURE_LOGGED_KEY,
        REALTIME_SESSION_SUCCESS_LOGGED_KEY,
    )

    protocols = [
        p.strip() for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()
    ]
    await websocket.accept(**({"subprotocol": protocols[0]} if protocols else {}))

    try:
        started = await read_start(websocket)
        if started is None:
            await reject(websocket, proxy, auth, 1000, "Client closed before session.start.")
            return
        event, model = started
        query_model = websocket.query_params.get("model")
        if query_model and query_model != model:
            raise LiveError("The model in the URL differs from session.model.")
        await authorize(proxy, auth, model)
    except LiveError as exc:
        await reject(websocket, proxy, auth, exc.code, str(exc))
        return
    except proxy.ProxyException as exc:
        proxy.log_model_access_denial(exc)
        await reject(websocket, proxy, auth, 1008, str(exc.message))
        return
    except Exception:  # noqa: BLE001 - details may contain request data
        logger.exception("GPT-Live session start failed")
        await reject(websocket, proxy, auth, 1011, "GPT-Live session start failed.")
        return

    websocket.scope[SCOPE_KEY] = {"alias": model, "start": event}
    scope = proxy.REALTIME_REQUEST_SCOPE_TEMPLATE.copy()
    scope["headers"] = list(websocket.scope.get("headers") or [])
    scope["path"] = websocket.scope.get("path", PATHS[0])
    request = Request(scope=scope)
    request._url = websocket.url

    async def body():
        return proxy.realtime_request_body(model)

    request.body = body
    data = {"model": model, "websocket": websocket, "query_params": {"model": model}}
    processing = proxy.ProxyBaseLLMRequestProcessing(data=data)
    try:
        data, logging_obj = await processing.common_processing_pre_call_logic(
            request=request,
            general_settings=proxy.general_settings,
            user_api_key_dict=auth,
            version=proxy.version,
            proxy_logging_obj=proxy.proxy_logging_obj,
            proxy_config=proxy.proxy_config,
            user_model=proxy.user_model,
            user_temperature=proxy.user_temperature,
            user_request_timeout=proxy.user_request_timeout,
            user_max_tokens=proxy.user_max_tokens,
            user_api_base=proxy.user_api_base,
            model=model,
            route_type="_arealtime",
        )
    except Exception as exc:  # noqa: BLE001 - budget, rate limit or guardrail
        logger.info("GPT-Live pre-call check rejected the session: %s", type(exc).__name__)
        message = getattr(exc, "message", None) or "GPT-Live pre-call check failed."
        await reject(websocket, proxy, auth, 1008, str(message))
        return
    except BaseException:
        await proxy._release_realtime_max_parallel_slot(auth)
        raise

    try:
        if data.get("model") != model or any(data.get(k) for k in OVERRIDES):
            raise LiveError("Changing the GPT-Live target in a pre-call hook is not allowed.", 1011)
        data.update(
            user_api_key_dict=auth,
            num_retries=0,
            disable_fallbacks=True,
            fallbacks=[],
            context_window_fallbacks=[],
            content_policy_fallbacks=[],
        )
        call = await proxy.route_request(
            data=data, route_type="_arealtime", llm_router=proxy.llm_router, user_model=None
        )
        await call
    except LiveError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_text(error_frame(str(exc)))
        with contextlib.suppress(Exception):
            await websocket.close(code=exc.code, reason=str(exc)[:120])
    except Exception:  # noqa: BLE001 - router errors may contain credentials
        logger.exception("GPT-Live routing failed")
        with contextlib.suppress(Exception):
            await websocket.send_text(error_frame("GPT-Live routing failed.", "server_error"))
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="GPT-Live routing failed.")
    finally:
        details = logging_obj.model_call_details
        if not details.get(REALTIME_SESSION_SUCCESS_LOGGED_KEY):
            await proxy._release_realtime_budget_reservation(auth)
            if not details.get(REALTIME_SESSION_FAILURE_LOGGED_KEY):
                await proxy._release_realtime_max_parallel_slot(auth)


MAX_BODY_BYTES = 1024 * 1024
# WebSocket close codes from policy map to HTTP status codes for the POST.
HTTP_STATUS = {1003: 400, 1007: 400, 1008: 400, 1009: 413, 1011: 500}


class _Carrier:
    """Stands in for the WebSocket on the router path of a WebRTC setup.

    The Azure realtime handler only reads the scope: the marker and the headers.
    """

    def __init__(self, scope):
        self.scope = scope
        self.headers = {}
        self.query_params = {}


def _error(status, message, kind="invalid_request_error"):
    return JSONResponse({"error": {"type": kind, "message": message}}, status_code=status)


async def authorize(proxy, auth, model):
    if proxy.llm_router is None:
        raise LiveError("GPT-Live needs a configured router.", 1011)
    await proxy.can_key_call_resolved_model(
        model=model,
        llm_model_list=proxy.llm_model_list,
        valid_token=auth,
        llm_router=proxy.llm_router,
    )
    deployments = proxy.llm_router.get_model_list(model_name=model) or []
    if not deployments or not all(is_azure(d) for d in deployments):
        raise LiveError(f"{model[:64]} is not an Azure GPT-Live model.")


def read_webrtc_request(raw):
    """Session and SDP offer from a POST body, checked like a session.start."""
    if len(raw) > MAX_BODY_BYTES:
        raise LiveError("Request body exceeds 1 MiB.", 1009)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise LiveError("Invalid JSON body.", 1007) from None
    if not isinstance(payload, dict):
        raise LiveError("Invalid JSON body.", 1007)
    transport = payload.get("transport")
    if not isinstance(transport, dict) or transport.get("type") != "webrtc":
        raise LiveError("Only transport type webrtc is supported here; use the WebSocket for audio through the proxy.")
    sdp = transport.get("sdp")
    if not isinstance(sdp, str) or not sdp or len(sdp.encode()) > webrtc.MAX_SDP_BYTES:
        raise LiveError("transport.sdp must be an SDP offer of at most 64 KiB.")
    session = payload.get("session")
    model = start_model({"type": "session.start", "session": session})
    return model, session, sdp


async def create_webrtc(request, auth, proxy):
    """POST /v1/live/sessions: set up a WebRTC session through the router."""
    from litellm.constants import (
        REALTIME_SESSION_FAILURE_LOGGED_KEY,
        REALTIME_SESSION_SUCCESS_LOGGED_KEY,
    )

    async def release():
        await proxy._release_realtime_budget_reservation(auth)
        await proxy._release_realtime_max_parallel_slot(auth)

    try:
        model, session, sdp = read_webrtc_request(await request.body())
        await authorize(proxy, auth, model)
    except LiveError as exc:
        await release()
        return _error(HTTP_STATUS.get(exc.code, 400), str(exc))
    except proxy.ProxyException as exc:
        proxy.log_model_access_denial(exc)
        await release()
        status = int(exc.code) if str(exc.code).isdigit() else 403
        return _error(status, str(exc.message))

    safety = request.headers.get("openai-safety-identifier")
    context = {
        "transport": "webrtc",
        "alias": model,
        "session": session,
        "sdp": sdp,
        "owner": webrtc.owner_of(auth),
        "safety_identifier": safety if safety and webrtc.SAFETY_IDENTIFIER.match(safety) else None,
    }
    carrier = _Carrier({"type": "http", "headers": list(request.scope.get("headers") or []), SCOPE_KEY: context})
    data = {"model": model, "websocket": carrier, "query_params": {"model": model}}
    processing = proxy.ProxyBaseLLMRequestProcessing(data=data)
    try:
        data, logging_obj = await processing.common_processing_pre_call_logic(
            request=request,
            general_settings=proxy.general_settings,
            user_api_key_dict=auth,
            version=proxy.version,
            proxy_logging_obj=proxy.proxy_logging_obj,
            proxy_config=proxy.proxy_config,
            user_model=proxy.user_model,
            user_temperature=proxy.user_temperature,
            user_request_timeout=proxy.user_request_timeout,
            user_max_tokens=proxy.user_max_tokens,
            user_api_base=proxy.user_api_base,
            model=model,
            route_type="_arealtime",
        )
    except Exception as exc:  # noqa: BLE001 - budget, rate limit or guardrail
        logger.info("GPT-Live pre-call check rejected the WebRTC session: %s", type(exc).__name__)
        await release()
        status = getattr(exc, "status_code", None)
        status = int(status) if str(status).isdigit() else 400
        return _error(status, str(getattr(exc, "message", None) or "GPT-Live pre-call check failed."))
    except BaseException:
        await proxy._release_realtime_max_parallel_slot(auth)
        raise

    try:
        if data.get("model") != model or any(data.get(k) for k in OVERRIDES):
            raise LiveError("Changing the GPT-Live target in a pre-call hook is not allowed.", 1011)
        data.update(
            user_api_key_dict=auth,
            num_retries=0,
            disable_fallbacks=True,
            fallbacks=[],
            context_window_fallbacks=[],
            content_policy_fallbacks=[],
        )
        call = await proxy.route_request(
            data=data, route_type="_arealtime", llm_router=proxy.llm_router, user_model=None
        )
        await call
    except LiveError as exc:
        context.setdefault("error", (HTTP_STATUS.get(exc.code, 500), {"error": {"type": "server_error", "message": str(exc)}}))
    except Exception:  # noqa: BLE001 - router errors may contain credentials
        logger.exception("GPT-Live WebRTC routing failed")
        context.setdefault("error", (502, {"error": {"type": "server_error", "message": "GPT-Live routing failed."}}))
    finally:
        if not context.get("owned"):
            details = logging_obj.model_call_details
            if not details.get(REALTIME_SESSION_SUCCESS_LOGGED_KEY):
                await proxy._release_realtime_budget_reservation(auth)
                if not details.get(REALTIME_SESSION_FAILURE_LOGGED_KEY):
                    await proxy._release_realtime_max_parallel_slot(auth)
    if "result" in context:
        return JSONResponse(context["result"], status_code=201)
    status, body = context.get("error") or (502, {"error": {"type": "server_error", "message": "GPT-Live setup returned nothing."}})
    return JSONResponse(body, status_code=status)


async def _deny(websocket, status):
    try:
        await websocket.send_denial_response(Response(status_code=status))
    except Exception:  # noqa: BLE001 - servers without the denial extension answer 403
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)


async def attach(websocket, session_id, auth):
    """WS /v1/live/sessions/{id}/attach: the control channel of a WebRTC session."""
    live = webrtc.SESSIONS.get(session_id)
    if live is None or live.finished.is_set():
        await _deny(websocket, 404)
        return
    if live.owner != webrtc.owner_of(auth):
        await _deny(websocket, 403)
        return
    if live.client is not None:
        await _deny(websocket, 409)
        return
    protocols = [
        p.strip() for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()
    ]
    await websocket.accept(**({"subprotocol": protocols[0]} if protocols else {}))
    await live.attach(websocket)
    with contextlib.suppress(Exception):
        await websocket.close(code=1000)


def register(app):
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth, user_api_key_auth_websocket

    existing = {getattr(route, "path", None) for route in app.routes}
    if existing.intersection(PATHS + ATTACH_PATHS):
        raise RuntimeError("GPT-Live route already exists.")

    async def live_sessions(
        websocket: WebSocket,
        user_api_key_dict=Depends(user_api_key_auth_websocket),
    ):
        from litellm.proxy import proxy_server

        await handle(websocket, user_api_key_dict, proxy_server)

    async def live_sessions_webrtc(request: Request, user_api_key_dict=Depends(user_api_key_auth)):
        from litellm.proxy import proxy_server

        return await create_webrtc(request, user_api_key_dict, proxy_server)

    async def live_session_attach(
        websocket: WebSocket,
        session_id: str,
        user_api_key_dict=Depends(user_api_key_auth_websocket),
    ):
        await attach(websocket, session_id, user_api_key_dict)

    for path in PATHS:
        app.add_api_websocket_route(path, live_sessions)
        app.add_api_route(path, live_sessions_webrtc, methods=["POST"], include_in_schema=False)
    for path in ATTACH_PATHS:
        app.add_api_websocket_route(path, live_session_attach)
