"""Read-only connection/cooldown eligibility for one saved subscription model."""

import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel


class MemberAvailability(BaseModel):
    subscription_id: str
    state: Literal[
        "eligible",
        "cooldown",
        "disconnected",
        "retired",
        "credential_rejected",
        "missing",
    ]
    retry_at: float | None = None


class ModelAvailability(BaseModel):
    model_id: str
    assignment_revision: str | None
    state: Literal["eligible", "unavailable", "paused"]
    checked_at: float
    cooldown_scope: Literal["worker", "shared"]
    members: list[MemberAvailability]


async def model_availability(parent, accounts):
    from litellm.proxy.proxy_server import llm_router
    from .model_assignment import ASSIGNMENT
    from .expansion import connection_eligible, member_cooldowns, member_model_id

    assignment = parent["model_info"].get(ASSIGNMENT)
    if not assignment:
        raise HTTPException(404, "This model has no managed subscription assignment.")
    if llm_router is None:
        raise HTTPException(503, "Routing status is unavailable.")
    model_id = parent["model_info"]["id"]
    identifiers = assignment["subscription_ids"]
    member_ids = [
        member_model_id(model_id, identifier, assignment["mode"])
        for identifier in identifiers
    ]
    # Fixed models retain native credential-error cooldowns; multi-member
    # routing instead fences the rejected token generation in Account state.
    cooldowns = await member_cooldowns(
        llm_router,
        member_ids,
        include_credential_rejections=assignment["mode"] == "fixed",
    )
    by_id = {
        account.status()["subscription_id"]: account.status() for account in accounts
    }
    members = []
    for identifier, member_id in zip(identifiers, member_ids):
        status = by_id.get(identifier)
        reason = "eligible"
        retry_at = None
        if status is None:
            reason = "missing"
        elif status["retired"]:
            reason = "retired"
        elif not connection_eligible(status):
            reason = (
                "credential_rejected"
                if status["state"] == "credential_rejected"
                else "disconnected"
            )
        elif member_id in cooldowns:
            reason = "cooldown"
            detail = cooldowns[member_id]
            retry_at = detail["timestamp"] + detail["cooldown_time"]
        members.append(
            MemberAvailability(
                subscription_id=identifier, state=reason, retry_at=retry_at
            )
        )
    return ModelAvailability(
        model_id=model_id,
        assignment_revision=assignment.get("revision"),
        state="paused"
        if parent["model_info"].get("blocked")
        else "eligible"
        if any(member.state == "eligible" for member in members)
        else "unavailable",
        checked_at=time.time(),
        cooldown_scope="shared"
        if llm_router.cache.redis_cache is not None
        else "worker",
        members=members,
    )
