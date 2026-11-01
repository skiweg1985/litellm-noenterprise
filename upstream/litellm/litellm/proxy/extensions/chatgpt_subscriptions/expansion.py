"""Expand one authoritative model into request-local native routing candidates."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from .accounts import Accounts
from .model_assignment import ASSIGNMENT, SubscriptionAssignment, _params

MEMBER = "_subscription_member"


def unavailable(
    message="No assigned subscription is available. Reconnect a subscription or retry after its cooldown.",
):
    import litellm

    return litellm.ServiceUnavailableError(
        message=message, model="subscription model", llm_provider="chatgpt"
    )


async def current_parent(parent):
    """No stale router copy may authorize changed or removed membership."""
    info = parent.get("model_info") or {}
    if not info.get("db_model"):
        return parent
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise unavailable("Subscription configuration is unavailable. Retry later.")
    row = await prisma_client.db.litellm_proxymodeltable.find_unique(
        where={"model_id": info["id"]}
    )
    if row is None or row.model_name != parent["model_name"]:
        raise unavailable("This model changed or was removed. Refresh the model list.")
    return {
        "model_name": row.model_name,
        "litellm_params": _params(row.litellm_params),
        "model_info": {**(row.model_info or {}), "id": row.model_id, "db_model": True},
    }


@dataclass(frozen=True)
class Selection:
    parent: dict
    revision: str
    subscription_id: str
    member_id: str


def record_selection(deployment, logging_obj):
    member = (deployment.get("model_info") or {}).get(MEMBER)
    if not member or logging_obj is None:
        return
    logging_obj._subscription_selection = Selection(
        member["parent"],
        member["revision"],
        member["subscription_id"],
        deployment["model_info"]["id"],
    )
    logging_obj._subscription_parent_model_id = member["parent"]["model_info"]["id"]


async def assert_dispatch_current(logging_obj):
    selection = getattr(logging_obj, "_subscription_selection", None)
    if not isinstance(selection, Selection):
        return
    parent = await current_parent(selection.parent)
    assignment = parent["model_info"].get(ASSIGNMENT) or {}
    if assignment.get(
        "revision"
    ) != selection.revision or selection.subscription_id not in assignment.get(
        "subscription_ids", []
    ):
        raise unavailable(
            "This subscription assignment changed. Retry the request with the current model configuration."
        )
    accounts = await asyncio.to_thread(Accounts)
    account = next(
        (
            a
            for a in accounts
            if a.status()["subscription_id"] == selection.subscription_id
        ),
        None,
    )
    if (
        account is None
        or account.status()["retired"]
        or account.status()["state"] in ("logged_out", "login_required")
    ):
        raise unavailable()


async def expand_candidates(router, candidates, request_kwargs=None):
    parents = [candidates] if isinstance(candidates, dict) else candidates
    managed = [p for p in parents if (p.get("model_info") or {}).get(ASSIGNMENT)]
    if not managed:
        return candidates
    if len(parents) != 1:
        raise unavailable(
            "This subscription model has conflicting deployments. Use a unique public model name."
        )
    parent = await current_parent(managed[0])
    assignment = SubscriptionAssignment.model_validate(
        parent["model_info"].get(ASSIGNMENT)
    )
    accounts = await asyncio.to_thread(Accounts)
    by_id = {a.status()["subscription_id"]: a for a in accounts}
    logging_obj = (request_kwargs or {}).get("litellm_logging_obj")
    from .attribution import snapshot

    attempted = {
        a["model_id"] for a in snapshot(logging_obj).get("subscription_attempts", [])
    }
    result = []
    for index, identifier in enumerate(assignment.subscription_ids):
        account = by_id.get(identifier)
        if account is None:
            continue
        status = account.status()
        if status["retired"] or not (
            status["token_valid"] or status["state"] == "token_expired"
        ):
            continue
        candidate = deepcopy(parent)
        model_id = (
            parent["model_info"]["id"]
            if assignment.mode == "fixed"
            else str(
                uuid5(NAMESPACE_URL, parent["model_info"]["id"] + ":" + identifier)
            )
        )
        if assignment.mode != "fixed" and model_id in attempted:
            continue
        candidate["model_info"]["id"] = model_id
        candidate["model_info"][MEMBER] = {
            "parent": parent,
            "revision": assignment.revision,
            "subscription_id": identifier,
        }
        candidate["litellm_params"].update(
            api_key="chatgpt-account:" + account.name, litellm_credential_name=None
        )
        if assignment.mode != "fixed":
            candidate["litellm_params"]["order"] = (
                index + 1 if assignment.mode == "priority" else 1
            )
        result.append(candidate)
    # Native cooldown store, explicitly queried for ephemeral member IDs rather
    # than only the parent's registered router ID.
    cooldowns = await router.cooldown_cache.async_get_active_cooldowns(
        model_ids=[d["model_info"]["id"] for d in result], parent_otel_span=None
    )
    blocked = {entry[0] for entry in (cooldowns or [])}
    result = [d for d in result if d["model_info"]["id"] not in blocked]
    if not result:
        raise unavailable()
    if request_kwargs is not None:
        for key in (
            "_target_order",
            "_excluded_deployment_ids",
            "_retry_skipped_deployment_ids",
            "routing_strategy",
            "_router_weights",
        ):
            request_kwargs.pop(key, None)
    return result


def choose_managed(candidates, model):
    if (
        not isinstance(candidates, list)
        or not candidates
        or not (candidates[0].get("model_info") or {}).get(MEMBER)
    ):
        return None
    mode = candidates[0]["model_info"][ASSIGNMENT]["mode"]
    if mode == "distribution":
        from .distribution import choose_distribution_deployment

        return choose_distribution_deployment(candidates, model)
    return candidates[0]
