"""Request-local subscription decisions at selection and at provider dispatch."""
import asyncio
import math
import time

import httpx
import litellm

from .access import Caller, Grants
from .admin import _credential_values, deployment_selector
from .attribution import CredentialSnapshot
from .fair_use import evaluate, quotas, response_headers
from .fair_use_math import quota_state, utc, window_name
from .registry import Registry


class SubscriptionAccessDenied(litellm.PermissionDeniedError):
    subscription_policy_denial = True

    def __init__(self):
        message = "No subscription is available to you for this model."
        super().__init__(message=message, llm_provider="chatgpt", model="subscription model",
                         response=httpx.Response(403, request=httpx.Request("POST", "https://litellm.invalid")))
        self.message = message
        self.type = "subscription_access_denied"


class FairUseLimitExceeded(litellm.RateLimitError):
    subscription_policy_denial = True

    def __init__(self, blocks):
        window, display = min(blocks, key=lambda block: block[0]["reset_at"])
        reset = window["reset_at"]
        message = f"The {window_name(window)} fair-use limit for {display} resets at {utc(reset)}."
        super().__init__(message=message, llm_provider="chatgpt", model="subscription model",
                         headers={"Retry-After": str(max(0, math.ceil(reset - time.time())))})
        self.message = message
        self.type = "fair_use_limit"


def selector(deployment):
    if not is_chatgpt(deployment):
        return None
    return deployment_selector(deployment["litellm_params"], _credential_values)[0]


def is_chatgpt(deployment):
    params = deployment.get("litellm_params") or {}
    return params.get("custom_llm_provider") == "chatgpt" or str(params.get("model", "")).startswith("chatgpt/")


async def decisions(registry, aliases, caller, eligible=None):
    records = registry["accounts"]
    factors = registry["fair_use_factors"]
    grants = await Grants.resolve([records[a] for a in aliases if a in records], callers=[caller])
    permitted = [a for a in aliases if a not in records or grants.permits(records[a]["access"], caller)]
    if not permitted:
        raise SubscriptionAccessDenied()
    allowed, blocks = {}, []
    for alias in permitted:
        if eligible is not None and alias not in eligible:
            continue
        record = records.get(alias)
        # Broken selectors retain the existing credential error at dispatch.
        if not record:
            allowed[alias] = {}
            continue
        snapshot = quotas.snapshots.get(record["subscription_id"])
        if (not caller.identity or not record["fair_use"]["enabled"]
                or quota_state(snapshot, time.time(), quotas.interval) != "current"):
            allowed[alias] = {}
            continue
        state, windows = await evaluate(record, factors, caller)
        blocking = [w for w in windows if w["users"][caller.identity]["throttled"]]
        if blocking:
            blocks.extend((w, record["display_name"]) for w in blocking)
        else:
            allowed[alias] = response_headers(windows, caller.identity) if state == "current" else {}
    if blocks and not allowed:
        raise FairUseLimitExceeded(blocks)
    return allowed


async def filter_config(candidates, data):
    single = isinstance(candidates, dict)
    rows = [candidates] if single else candidates
    aliases = [selector(d) if not (d.get("model_info") or {}).get("subscription_assignment") else None for d in rows]
    if not any(aliases):
        return candidates
    registry = await asyncio.to_thread(Registry().read)
    allowed = await decisions(registry, dict.fromkeys(aliases), Caller.from_request(data))
    filtered = [row for row, alias in zip(rows, aliases) if alias in allowed]
    return filtered[0] if single else filtered


async def dispatch(logging_obj, credential):
    if not isinstance(credential, CredentialSnapshot):
        return
    logging_obj._subscription_fair_use_headers = {}
    registry = await asyncio.to_thread(Registry().read)
    alias = next((a for a, r in registry["accounts"].items() if r["subscription_id"] == credential.subscription_id), None)
    if alias is None:
        raise SubscriptionAccessDenied()
    details = logging_obj.model_call_details
    data = {**(details.get("litellm_params") or {}), "litellm_logging_obj": logging_obj}
    caller = Caller.from_request(data)
    logging_obj._subscription_caller = caller
    headers = (await decisions(registry, [alias], caller))[alias]
    snapshot = quotas.snapshots.get(credential.subscription_id)
    if headers and quota_state(snapshot, time.time(), quotas.interval) == "current":
        logging_obj._subscription_headers_until = min(snapshot["fetched_at"] + 3 * quotas.interval,
                                                       snapshot["primary"]["reset_at"], snapshot["secondary"]["reset_at"])
        logging_obj._subscription_fair_use_headers = headers


async def visible_deployments(deployments, auth, management=False):
    if management and auth.user_role == "proxy_admin":
        return deployments
    if not any(isinstance(d, dict) and is_chatgpt(d) for d in deployments):
        return deployments
    registry = await asyncio.to_thread(Registry().read)
    records = registry["accounts"]
    by_id = {r["subscription_id"]: r for r in records.values()}
    caller = Caller.from_auth(auth)
    grants = await Grants.resolve(records.values(), callers=[caller])
    visible = []
    for deployment in deployments:
        if not isinstance(deployment, dict):
            visible.append(deployment)
            continue
        assignment = (deployment.get("model_info") or {}).get("subscription_assignment")
        if assignment:
            members = [by_id[i] for i in assignment.get("subscription_ids", []) if i in by_id]
        else:
            alias = selector(deployment)
            if alias is None:
                visible.append(deployment)
                continue
            members = [records[alias]] if alias in records else []
        if any(not r["retired"] and grants.permits(r["access"], caller) for r in members):
            visible.append(deployment)
    return visible


async def hidden_models(router, auth):
    if router is None:
        return set()
    rows = router.get_model_list() or []
    visible = await visible_deployments(rows, auth)
    return {d["model_name"] for d in rows} - {d["model_name"] for d in visible}
