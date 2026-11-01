"""Admin page and API for the ChatGPT sign-in, for LiteLLM proxy admins only."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from . import tokens
from .service import MonitoringError, UsageService, ModelsService

logger = logging.getLogger("chatgpt_subscriptions")

API = "/chatgpt/api/accounts"
PAGE = "/chatgpt/ui"
NO_STORE = {"Cache-Control": "no-store"}



class SubscriptionName(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class SubscriptionDescription(BaseModel):
    """Stable public management fields; provider status details remain additive."""
    model_config = ConfigDict(extra="allow")
    account: str
    subscription_id: str
    display_name: str
    connection_revision: str | None
    retired: bool
    state: str
    token_valid: bool
    models: list[str]
    deployment_ids: list[str] = Field(default_factory=list)


class SubscriptionList(BaseModel):
    accounts: list[SubscriptionDescription]
    problems: list[dict]


def litellm_admin():
    """LiteLLM's own key check; the UI session key or any admin key passes."""
    try:
        from litellm.proxy._types import LitellmUserRoles
        from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
    except ImportError:

        def unavailable():
            raise HTTPException(503, "Admin authentication unavailable.")

        return unavailable

    admin_roles = {LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.PROXY_ADMIN.value}

    def require_admin(auth=Depends(user_api_key_auth)):
        if getattr(auth, "oidc_identity_source", None) == "provider":
            raise HTTPException(403, "Use an administrator browser session or native admin key.")
        if getattr(auth, "user_role", None) not in admin_roles:
            raise HTTPException(403, "Proxy admin role required.")
        return auth

    return require_admin


# Configuration problems of a ChatGPT deployment; each one fails with 401.
CREDENTIAL_MISSING = "credential_missing"
NO_SUBSCRIPTION = "no_subscription"
UNKNOWN_SUBSCRIPTION = "unknown_subscription"


def resolve_deployments(model_list, credential_values, known):
    """ChatGPT deployments with model name, id, credential and subscription.

    The subscription comes from api_key "chatgpt-account:<name>", set directly
    or through the credential; LiteLLM resolves credentials only when it calls
    the deployment, so the model list does not carry the api_key.
    """
    found = []
    for deployment in model_list or []:
        params = deployment.get("litellm_params") or {}
        if not str(params.get("model", "")).startswith("chatgpt/"):
            continue
        credential = params.get("litellm_credential_name") or None
        api_key, problem = params.get("api_key"), None
        if api_key is None and credential:
            values = credential_values(credential)
            if values is None:
                problem = CREDENTIAL_MISSING
            else:
                api_key = values.get("api_key")
        account = tokens.account_for_api_key(api_key)
        if problem is None and account is None:
            problem = NO_SUBSCRIPTION
        elif problem is None and account not in known:
            problem = UNKNOWN_SUBSCRIPTION
        found.append({
            "model_name": deployment.get("model_name"),
            "slug": str(params["model"]).split("/", 1)[1],
            "id": (deployment.get("model_info") or {}).get("id"),
            "credential": credential,
            "account": account,
            "problem": problem,
        })
    return found


def _credential_values(name):
    import litellm

    for credential in litellm.credential_list or []:
        if credential.credential_name == name:
            return dict(credential.credential_values or {})
    return None


def chatgpt_deployments():
    """ChatGPT deployments of the running proxy, see resolve_deployments."""
    try:
        from litellm.proxy.proxy_server import llm_router
    except ImportError:
        return []
    if llm_router is None:
        return []
    # model_list holds the deployments only; get_model_list() adds routing groups.
    return resolve_deployments(
        llm_router.model_list, _credential_values, set(tokens.configured_accounts())
    )


def problems(deployments):
    """Broken ChatGPT deployments, without secrets."""
    return [
        {key: d[key] for key in ("model_name", "id", "credential", "account", "problem")}
        | {"deployment": d["model_name"]}
        for d in deployments
        if d["problem"]
    ]


class RequestCounter:
    """ChatGPT requests of the last 24 hours per deployment id, cached."""

    def __init__(self, ttl=60):
        self.ttl = ttl
        self.value = None
        self.until = 0

    async def __call__(self):
        if time.monotonic() < self.until:
            return self.value
        self.value = None
        try:
            from litellm.proxy.proxy_server import prisma_client

            if prisma_client is not None:
                rows = await prisma_client.db.query_raw(
                    'SELECT model_id, COUNT(*)::int AS n FROM "LiteLLM_SpendLogs" '
                    "WHERE custom_llm_provider = $1 "
                    "AND \"startTime\" > NOW() - INTERVAL '24 hours' "
                    "GROUP BY model_id",
                    "chatgpt",
                )
                self.value = {row["model_id"]: row["n"] for row in rows}
        except Exception:  # noqa: BLE001 - the count is optional information
            self.value = None
        self.until = time.monotonic() + self.ttl
        return self.value


def catalog(models, own):
    """The subscription's models for the page, each marked if LiteLLM has a deployment.

    Hidden models only appear when a deployment uses them. `missing` lists
    deployments whose model is not in the subscription's list at all.
    """
    configured = {d["slug"] for d in own}
    offered = {m["slug"] for m in models}
    return {
        "models": [
            {**m, "configured": m["slug"] in configured}
            for m in models if m["visible"] or m["slug"] in configured
        ],
        "missing": sorted(
            ({"model_name": d["model_name"], "slug": d["slug"]} for d in own if d["slug"] not in offered),
            key=lambda d: (str(d["model_name"]), d["slug"]),
        ),
    }


def router(accounts, services, dependency=None, deployments=None, requests=None, catalogs=None):
    deployments = deployments or chatgpt_deployments
    catalogs = catalogs or {}
    requests = requests or RequestCounter()
    dependency = dependency or litellm_admin()
    api = APIRouter(dependencies=[Depends(dependency)], tags=["ChatGPT sign-in"])
    public = APIRouter(tags=["ChatGPT sign-in"])

    @public.get(PAGE, include_in_schema=False)
    async def ui():
        return RedirectResponse("/ui/chatgpt-subscriptions/", status_code=307, headers=NO_STORE)

    async def describe(account):
        services.setdefault(account.name, UsageService(account=account.name, path=account.path, transport=account.transport))
        catalogs.setdefault(account.name, ModelsService(account=account.name, path=account.path, transport=account.transport))
        status = account.status()
        quota, quota_error = None, None
        if status["token_valid"]:
            try:
                quota = await services[account.name].get()
            except MonitoringError as exc:
                quota_error = str(exc)
        own = [d for d in deployments() if d["account"] == account.name]
        available, available_error = None, None
        if status["token_valid"] and account.name in catalogs:
            try:
                available = catalog((await catalogs[account.name].get())["models"], own)
            except MonitoringError as exc:
                available_error = str(exc)
        counts = await requests()
        return {
            **status,
            "quota": quota,
            "quota_error": quota_error,
            "available_models": available,
            "available_models_error": available_error,
            "credentials": sorted({d["credential"] for d in own} - {None}),
            "models": sorted({d["model_name"] for d in own} - {None}),
            "deployment_ids": sorted({d["id"] for d in own} - {None}),
            "requests_24h": None if counts is None else sum(counts.get(d["id"], 0) for d in own),
        }

    def select(name, action, auth):
        account = accounts.get(name)
        if account is None:
            raise HTTPException(404, "Unknown ChatGPT account.")
        if account.retired() and action != "retired":
            raise HTTPException(409, "Retired subscriptions cannot be changed.")
        # Audit trail in the container log; never contains tokens.
        actor = getattr(auth, "user_email", None) or getattr(auth, "user_id", None) or "unknown"
        logger.warning("ChatGPT account %s: %s by %s", name, action, actor)
        return account

    @api.get(API, response_model=SubscriptionList)
    async def list_accounts():
        return JSONResponse(
            {
                "accounts": [await describe(account) for account in accounts],
                "problems": problems(deployments()),
            },
            headers=NO_STORE,
        )

    @api.post(API, response_model=SubscriptionDescription)
    async def create_account(body: SubscriptionName, auth=Depends(dependency)):
        try:
            account = await asyncio.to_thread(accounts.create, body.display_name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.patch(API + "/{name}", response_model=SubscriptionDescription)
    async def rename_account(name: str, body: SubscriptionName, auth=Depends(dependency)):
        select(name, "renamed", auth)
        try:
            account = await asyncio.to_thread(accounts.rename, name, body.display_name)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.post(API + "/{name}/retire", response_model=SubscriptionDescription)
    async def retire_account(name: str, auth=Depends(dependency)):
        select(name, "retired", auth)
        account = await accounts.retire(name)
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.post(API + "/{name}/login", response_model=SubscriptionDescription)
    async def start_login(name: str, auth=Depends(dependency)):
        account = select(name, "sign-in started", auth)
        account.start_login()
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.post(API + "/{name}/login/cancel", response_model=SubscriptionDescription)
    async def cancel_login(name: str, auth=Depends(dependency)):
        account = select(name, "sign-in cancelled", auth)
        await account.cancel_login()
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.post(API + "/{name}/pending/confirm", response_model=SubscriptionDescription)
    async def confirm_pending(name: str, auth=Depends(dependency)):
        account = select(name, "different account confirmed", auth)
        if not await account.confirm_pending():
            raise HTTPException(409, "Pending sign-in is unavailable or conflicts with another subscription.")
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.post(API + "/{name}/pending/discard", response_model=SubscriptionDescription)
    async def discard_pending(name: str, auth=Depends(dependency)):
        account = select(name, "different account discarded", auth)
        await account.discard_pending()
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.post(API + "/{name}/refresh", response_model=SubscriptionDescription)
    async def refresh(name: str, auth=Depends(dependency)):
        account = select(name, "token refresh", auth)
        await account.refresh_now()
        return JSONResponse(await describe(account), headers=NO_STORE)

    @api.post(API + "/{name}/logout", response_model=SubscriptionDescription)
    async def logout(name: str, auth=Depends(dependency)):
        account = select(name, "sign-out", auth)
        await account.logout()
        return JSONResponse(await describe(account), headers=NO_STORE)

    return public, api
