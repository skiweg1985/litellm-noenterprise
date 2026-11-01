"""Register the monitoring and sign-in routes, each with its own authentication."""

import asyncio
import contextlib
import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from . import admin, observed_usage
from .accounts import Accounts
from .prtg import login_text, render
from .service import ModelsService, MonitoringError, UsageService

OVERVIEW = "/chatgpt/accounts"
PREFIX = OVERVIEW + "/{name}/usage"
UNKNOWN = "Unknown ChatGPT account."
PATHS = (OVERVIEW, PREFIX, PREFIX + "/prtg", admin.PAGE, admin.API, observed_usage.API)
# Fields of an account that the monitoring key may see: no email, no ids.
OVERVIEW_FIELDS = ("account", "state", "token_valid", "plan", "token_expires_at",
                   "last_refresh_at", "refresh_rejected", "auto_login")


def configuration_text(found):
    names = ", ".join(sorted({p["deployment"] or p["id"] or "?" for p in found}))
    return (f"Configuration: {len(found)} ChatGPT deployment(s) without a valid "
            f"subscription ({names}). Requests to them fail. See /chatgpt/ui")


def require_key(x_monitoring_key: str = Header(default="")):
    expected = os.getenv("CHATGPT_MONITORING_API_KEY", "")
    if not expected:
        raise HTTPException(503, "ChatGPT monitoring is disabled.")
    if not secrets.compare_digest(x_monitoring_key.encode(), expected.encode()):
        raise HTTPException(401, "Invalid monitoring credentials.")


def watch_with_server(app, accounts):
    """Run the token watcher only while the server runs, not on a mere import."""
    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application):
        async with original(application) as state:
            task = asyncio.ensure_future(accounts.run())
            try:
                yield state
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await accounts.stop_logins()

    app.router.lifespan_context = lifespan


def register(app, accounts=None, services=None, admin_dependency=None, watch=True, catalogs=None):
    """services and catalogs map account names to quota and models lookups; one per account by default."""
    deployments = admin.chatgpt_deployments
    if getattr(app.state, "chatgpt_subscriptions_registered", False) or any(
        getattr(route, "path", "") in PATHS for route in app.routes
    ):
        raise RuntimeError("ChatGPT monitoring route collision.")
    accounts = accounts if accounts is not None else Accounts()
    services = services or {
        account.name: UsageService(account=account.name, path=account.path, transport=account.transport) for account in accounts
    }
    catalogs = catalogs or {
        account.name: ModelsService(account=account.name, path=account.path, transport=account.transport) for account in accounts
    }
    router = APIRouter(dependencies=[Depends(require_key)], tags=["ChatGPT monitoring"])

    @router.get(OVERVIEW)
    async def overview():
        """Master data and quota of every subscription, for the cost dashboard."""
        found = deployments()
        rows = []
        for account in accounts:
            services.setdefault(account.name, UsageService(account=account.name, path=account.path, transport=account.transport))
            status = account.status()
            own = [d for d in found if d["account"] == account.name and not d["problem"]]
            quota, quota_error = None, None
            if status["token_valid"]:
                try:
                    quota = await services[account.name].get()
                except MonitoringError as exc:
                    quota_error = str(exc)
            rows.append({
                **{key: status.get(key) for key in OVERVIEW_FIELDS},
                "credentials": sorted({d["credential"] for d in own} - {None}),
                "models": sorted({d["model_name"] for d in own} - {None}),
                "deployment_ids": sorted({d["id"] for d in own} - {None}),
                "quota": quota,
                "quota_error": quota_error,
            })
        return JSONResponse({"accounts": rows, "problems": admin.problems(found)},
                            headers={"Cache-Control": "no-store"})

    @router.get(PREFIX)
    async def usage(name: str):
        account = accounts.get(name)
        if account is None:
            return JSONResponse({"error": UNKNOWN}, status_code=404, headers={"Cache-Control": "no-store"})
        if account.retired():
            raise HTTPException(409, "Subscription is retired.")
        services.setdefault(name, UsageService(account=name, path=account.path, transport=account.transport))
        try:
            result = await services[name].get()
        except MonitoringError as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get(PREFIX + "/prtg")
    async def prtg(
        name: str,
        limit_id: str = Query(default="codex", max_length=128),
        windows: str = Query(default="primary,secondary", max_length=32),
    ):
        account = accounts.get(name)
        if account is None:
            return JSONResponse({"prtg": {"error": 1, "text": UNKNOWN}}, status_code=404,
                                headers={"Cache-Control": "no-store"})
        services.setdefault(name, UsageService(account=name, path=account.path, transport=account.transport))
        status = account.status()
        broken = admin.problems(deployments())
        # Alarm when requests fail now or once the token expires, and while a
        # sign-in with a different account waits for an admin.
        if broken:
            result = {"prtg": {"error": 1, "text": configuration_text(broken)}}
        elif status["pending_account"]:
            result = {"prtg": {"error": 1, "text": (
                "A sign-in with a different ChatGPT account waits for confirmation on /chatgpt/ui"
            )}}
        elif not status["token_valid"] or status["refresh_rejected"]:
            result = {"prtg": {"error": 1, "text": login_text(status)}}
        else:
            try:
                result = render(await services[name].get(), limit_id, windows)
                if status["state"] == "login_pending":
                    result["prtg"]["text"] += " | " + login_text(status)
            except MonitoringError as exc:
                result = {"prtg": {"error": 1, "text": str(exc)}}
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    app.include_router(router)
    app.include_router(observed_usage.router(accounts, admin_dependency))
    for part in admin.router(accounts, services, admin_dependency, deployments=deployments, catalogs=catalogs):
        app.include_router(part)
    if watch:
        watch_with_server(app, accounts)
    app.state.chatgpt_subscriptions_registered = True
    return accounts
