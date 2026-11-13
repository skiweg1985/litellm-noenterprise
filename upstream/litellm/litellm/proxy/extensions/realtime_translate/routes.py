"""WebSocket route /v1/realtime/translations with LiteLLM's own key, budget and limit checks.

The route follows LiteLLM's /v1/realtime endpoint: the model alias comes from
?model=, the key must be allowed to call it, the pre-call checks run and the
router picks the deployment. The Azure realtime handler then hands the session
to stream.run_azure instead of its turn-based relay.
"""

import contextlib
import logging

from fastapi import Depends, Request, WebSocket

from .policy import (
    PATHS,
    SCOPE_KEY,
    TranslationError,
    check_deployment,
    error_frame,
    requested_model,
)

logger = logging.getLogger(__name__)

# Request fields that would bypass the router's choice of deployment.
OVERRIDES = ("api_base", "base_url", "api_key", "azure_ad_token", "api_version")


async def reject(websocket, proxy, auth, code, message):
    try:
        with contextlib.suppress(Exception):
            await websocket.send_text(error_frame(message))
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=message[:120])
    finally:
        await proxy._release_realtime_budget_reservation(auth)
        await proxy._release_realtime_max_parallel_slot(auth)


async def authorize(proxy, auth, model):
    if proxy.llm_router is None:
        raise TranslationError("Translation needs a configured router.", 1011)
    await proxy.can_key_call_resolved_model(
        model=model,
        llm_model_list=proxy.llm_model_list,
        valid_token=auth,
        llm_router=proxy.llm_router,
    )
    deployments = proxy.llm_router.get_model_list(model_name=model) or []
    if not deployments:
        raise TranslationError("Translation model is not configured.")
    for deployment in deployments:
        check_deployment(deployment)


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
        model = requested_model(websocket.query_params)
        await authorize(proxy, auth, model)
    except TranslationError as exc:
        await reject(websocket, proxy, auth, exc.code, str(exc))
        return
    except proxy.ProxyException as exc:
        proxy.log_model_access_denial(exc)
        await reject(websocket, proxy, auth, 1008, str(exc.message))
        return
    except Exception:  # noqa: BLE001 - details may contain request data
        logger.exception("Translation session start failed")
        await reject(websocket, proxy, auth, 1011, "Translation session start failed.")
        return

    websocket.scope[SCOPE_KEY] = {"alias": model}
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
        logger.info("Translation pre-call check rejected the session: %s", type(exc).__name__)
        message = getattr(exc, "message", None) or "Translation pre-call check failed."
        await reject(websocket, proxy, auth, 1008, str(message))
        return
    except BaseException:
        await proxy._release_realtime_max_parallel_slot(auth)
        raise

    try:
        if data.get("model") != model or any(data.get(k) for k in OVERRIDES):
            raise TranslationError("Changing the translation target in a pre-call hook is not allowed.", 1011)
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
    except TranslationError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_text(error_frame(str(exc)))
        with contextlib.suppress(Exception):
            await websocket.close(code=exc.code, reason=str(exc)[:120])
    except Exception:  # noqa: BLE001 - router errors may contain credentials
        logger.exception("Translation routing failed")
        with contextlib.suppress(Exception):
            await websocket.send_text(error_frame("Translation routing failed."))
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="Translation routing failed.")
    finally:
        details = logging_obj.model_call_details
        if not details.get(REALTIME_SESSION_SUCCESS_LOGGED_KEY):
            await proxy._release_realtime_budget_reservation(auth)
            if not details.get(REALTIME_SESSION_FAILURE_LOGGED_KEY):
                await proxy._release_realtime_max_parallel_slot(auth)


def register(app):
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth_websocket

    existing = {getattr(route, "path", None) for route in app.routes}
    if existing.intersection(PATHS):
        raise RuntimeError("Translation route already exists.")

    async def realtime_translations(
        websocket: WebSocket,
        user_api_key_dict=Depends(user_api_key_auth_websocket),
    ):
        from litellm.proxy import proxy_server

        await handle(websocket, user_api_key_dict, proxy_server)

    for path in PATHS:
        app.add_api_websocket_route(path, realtime_translations)
