"""Administration of subscription grants and the authenticated caller's quota view."""
import asyncio
import json
import sys
import time
from typing import Literal

import litellm
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from litellm.proxy._types import ProxyException
from pydantic import BaseModel

from .access import Caller, Grants
from .admin import API, NO_STORE, litellm_admin
from .counters import counters
from .enforcement import is_chatgpt
from .fair_use import evaluate, quotas
from .fair_use_math import WINDOWS, quota_state
from .fair_use_weights import ModelPricing, model_id
from .settings import Access, Factors, FairUse, ModelWeights


class UserShare(BaseModel):
    share: float
    allowance: float
    factor: float
    tokens: int
    consumption: float
    throttled: bool


class ActiveUser(UserShare):
    user_id: str


class WindowInfo(BaseModel):
    id: Literal["primary", "secondary"]
    used_percent: float | None
    threshold: int
    scarce: bool
    reset_at: float | None


class AdminWindow(WindowInfo):
    window_seconds: float | None
    users: list[ActiveUser]


class PersonalWindow(WindowInfo, UserShare):
    pass


class AccountInfo(BaseModel):
    display_name: str
    enabled: bool
    quota_state: Literal["unavailable", "stale", "current"]


class AdminAccount(AccountInfo):
    account: str
    subscription_id: str
    access: Access
    windows: list[AdminWindow]
    unknown_ids: Access


class PersonalAccount(AccountInfo):
    windows: list[PersonalWindow]


class Status(BaseModel):
    refreshed_at: float | None
    accounts: list[AdminAccount]


class PersonalStatus(BaseModel):
    user_id: str
    accounts: list[PersonalAccount]


class ModelWeightInfo(BaseModel):
    model: str
    input: float | None
    output: float | None
    cache_read: float | None
    source: Literal["list", "like", "factor", "prices", "unpriced"]
    weight: float
    unpriced: bool


class ModelWeightsStatus(BaseModel):
    reference_model: str
    reference_fallback: bool
    overrides: ModelWeights
    models: list[ModelWeightInfo]


async def model_weights_status(overrides):
    router = getattr(sys.modules.get("litellm.proxy.proxy_server"), "llm_router", None)
    models = {model_id(row["litellm_params"].get("model")) for row in (router.model_list if router else [])
              if is_chatgpt(row)}
    now = time.time()
    for account, snapshot in list(quotas.snapshots.items()):
        if quota_state(snapshot, now, quotas.interval) == "unavailable":
            continue
        for name in WINDOWS:
            # An older quota reading still identifies the window until its reset.
            if snapshot[name]["reset_at"] > now:
                users = await counters.counts(account, name, snapshot[name])
                for row in users.values():
                    models.update(row["models"])
    models.discard(None)
    return ModelPricing(overrides, litellm.model_cost).status(models)


async def status(registry, caller=None):
    value = await asyncio.to_thread(registry.read)
    records = value["accounts"]
    factors = value["fair_use_factors"]
    grants = await Grants.resolve(records.values(), factors, [caller] if caller else [])
    accounts = []
    for alias, record in records.items():
        if caller and (record["retired"] or not grants.permits(record["access"], caller)):
            continue
        state, windows = await evaluate(record, factors, value["model_weights"], caller)
        account = dict(display_name=record["display_name"], enabled=record["fair_use"]["enabled"], quota_state=state)
        if caller:
            own = []
            for window in windows:
                row = window.pop("users")[caller.identity]
                own.append(PersonalWindow(**window, **row))
            accounts.append(PersonalAccount(**account, windows=own))
        else:
            for window in windows:
                window["users"] = list(window["users"].values())
            accounts.append(AdminAccount(**account, account=alias, subscription_id=record["subscription_id"],
                                         access=record["access"], windows=windows, unknown_ids=grants.unknown(record["access"])))
    if caller:
        return PersonalStatus(user_id=caller.identity, accounts=accounts)
    return Status(refreshed_at=quotas.refreshed_at, accounts=accounts)


async def audit(identifier, field, old, saved, auth):
    from litellm.constants import LITELLM_PROXY_ADMIN_NAME
    from litellm.proxy._types import LitellmTableNames
    from litellm.proxy.management_helpers.audit_logs import create_object_audit_log

    await create_object_audit_log(
        object_id=identifier, action="updated", litellm_changed_by=None, user_api_key_dict=auth,
        litellm_proxy_admin_name=LITELLM_PROXY_ADMIN_NAME,
        table_name=LitellmTableNames.SUBSCRIPTION_REGISTRY,
        before_value=json.dumps({field: old}), after_value=json.dumps({field: saved}),
    )


class NoStoreRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def no_store(request: Request):
            try:
                response = await handle(request)
            except RequestValidationError:
                raise HTTPException(422, "Invalid subscription settings.", headers=NO_STORE) from None
            except (HTTPException, ProxyException) as exc:
                exc.headers = {**(exc.headers or {}), **NO_STORE}
                raise
            response.headers.update(NO_STORE)
            return response

        return no_store


def router(accounts, admin_dependency=None):
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

    api = APIRouter(tags=["ChatGPT fair use"], route_class=NoStoreRoute)
    admin = admin_dependency or litellm_admin()
    registry = accounts.registry
    weights_path = "/chatgpt/api/fair-use/model-weights"
    factors_path = "/chatgpt/api/fair-use/factors"

    async def read(field, name=None):
        value = await asyncio.to_thread(registry.read)
        if name is None:
            return value[field]
        if name not in value["accounts"]:
            raise HTTPException(404, "Unknown ChatGPT account.", headers=NO_STORE)
        return value["accounts"][name][field]

    async def write(field, body, auth, name=None):
        try:
            old, saved = await asyncio.to_thread(registry.update_settings, field, body, name)
        except KeyError:
            raise HTTPException(404, "Unknown ChatGPT account.", headers=NO_STORE) from None
        except ValueError:
            raise HTTPException(409, "Subscription settings cannot be changed.", headers=NO_STORE) from None
        await audit(name or field, field, old, saved, auth)
        return saved

    @api.get(API + "/{name}/access", response_model=Access)
    async def get_access(name: str, auth=Depends(admin)):
        return await read("access", name)

    @api.put(API + "/{name}/access", response_model=Access)
    async def set_access(name: str, body: Access, auth=Depends(admin)):
        return await write("access", body, auth, name)

    @api.get(API + "/{name}/fair-use", response_model=FairUse)
    async def get_policy(name: str, auth=Depends(admin)):
        return await read("fair_use", name)

    @api.put(API + "/{name}/fair-use", response_model=FairUse)
    async def set_policy(name: str, body: FairUse, auth=Depends(admin)):
        return await write("fair_use", body, auth, name)

    @api.get(factors_path, response_model=Factors)
    async def get_factors(auth=Depends(admin)):
        return await read("fair_use_factors")

    @api.put(factors_path, response_model=Factors)
    async def set_factors(body: Factors, auth=Depends(admin)):
        return await write("fair_use_factors", body, auth)

    @api.get(weights_path, response_model=ModelWeightsStatus)
    async def get_model_weights(auth=Depends(admin)):
        return await model_weights_status(await read("model_weights"))

    @api.put(weights_path, response_model=ModelWeightsStatus)
    async def set_model_weights(body: ModelWeights, auth=Depends(admin)):
        return await model_weights_status(await write("model_weights", body, auth))

    @api.get("/chatgpt/api/fair-use/status", response_model=Status)
    async def admin_status(auth=Depends(admin)):
        return await status(registry)

    @api.get("/chatgpt/fair-use/me", response_model=PersonalStatus)
    async def me(auth=Depends(user_api_key_auth)):
        caller = Caller.from_auth(auth)
        if not caller.identity:
            raise HTTPException(401, "Authenticated identity required.", headers=NO_STORE)
        return await status(registry, caller)

    return api
