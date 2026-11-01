"""Expand one authoritative model into request-local native routing candidates."""

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from .accounts import Accounts
from .model_assignment import ASSIGNMENT, SubscriptionAssignment, _params

MEMBER = "_subscription_member"


def capacity_model_id(model_info):
    """Keep the single configured model's native quotas shared by all members."""
    member = model_info.get(MEMBER)
    if member:
        return member["parent"]["model_info"]["id"]
    return model_info.get("id")


def capacity_deployment(deployment):
    member = (deployment.get("model_info") or {}).get(MEMBER)
    return member["parent"] if member else deployment


def unavailable(
    message="No assigned subscription is available. Reconnect a subscription or retry after its cooldown.",
):
    import litellm

    return litellm.ServiceUnavailableError(
        message=message, model="subscription model", llm_provider="chatgpt"
    )


async def hydrate_missing_parent(router, model):
    """Make a newly saved managed parent usable on a sibling worker immediately."""
    name = router.get_model_from_alias(model=model) or model
    if name in router.model_names or router.get_deployment(model_id=name) is not None:
        return
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        return
    rows = await prisma_client.db.litellm_proxymodeltable.find_many(
        where={"OR": [{"model_name": name}, {"model_id": name}]}
    )
    from litellm.types.router import Deployment, LiteLLM_Params, ModelInfo

    for row in rows:
        info = row.model_info or {}
        if not info.get(ASSIGNMENT):
            continue
        # Register only the parent, then let normal native team/auth routing
        # select it. Ephemeral members never enter the public model inventory.
        router.upsert_deployment(
            Deployment(
                model_name=row.model_name,
                litellm_params=LiteLLM_Params(**_params(row.litellm_params)),
                model_info=ModelInfo(
                    **{
                        **info,
                        "id": row.model_id,
                        "db_model": True,
                        "blocked": bool(row.blocked),
                    }
                ),
            )
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
        "model_info": {
            **(row.model_info or {}),
            "id": row.model_id,
            "db_model": True,
            "blocked": bool(row.blocked),
        },
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
    if parent["model_info"].get("blocked"):
        raise unavailable("This model is paused. Resume it before sending requests.")
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
        or account.status()["state"]
        in ("logged_out", "login_required", "credential_rejected")
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
    policy = getattr(
        (request_kwargs or {}).get("litellm_logging_obj"), "_subscription_policy", None
    )
    if policy is not None and (
        policy.revision != assignment.revision or time.monotonic() >= policy.deadline
    ):
        raise unavailable(
            "The assignment changed or its request timeout expired. Retry with the current model configuration."
        )
    if policy is not None and request_kwargs is not None:
        remaining = max(0.001, policy.deadline - time.monotonic())
        for key in ("timeout", "request_timeout", "stream_timeout"):
            supplied = request_kwargs.get(key)
            request_kwargs[key] = (
                min(supplied, remaining)
                if isinstance(supplied, (int, float)) and supplied > 0
                else remaining
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
            "member_id": model_id,
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
    # A credential rejection is tied to the token fingerprint above. A newly
    # authorized generation recovers immediately instead of inheriting that 401.
    blocked = {
        entry[0]
        for entry in (cooldowns or [])
        if str(entry[1].get("status_code")) not in ("401", "403")
    }
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
