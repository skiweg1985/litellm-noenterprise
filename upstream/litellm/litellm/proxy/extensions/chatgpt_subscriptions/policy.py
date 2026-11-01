"""Bound native retry/cooldown behavior to one managed subscription assignment."""

import time
from dataclasses import dataclass

from .expansion import Selection, current_parent, hydrate_missing_parent
from .model_assignment import ASSIGNMENT, SubscriptionAssignment


@dataclass(frozen=True)
class Policy:
    parent_id: str
    revision: str
    retries: int
    deadline: float


async def retry_policy(router, model, kwargs):
    await hydrate_missing_parent(router, model)
    _, parents = router._common_checks_available_deployment(
        model=model, request_kwargs=kwargs
    )
    parents = [parents] if isinstance(parents, dict) else parents
    managed = [p for p in parents if (p.get("model_info") or {}).get(ASSIGNMENT)]
    if len(managed) != 1 or managed[0]["model_info"][ASSIGNMENT].get("mode") == "fixed":
        return None
    parent = await current_parent(managed[0])
    assignment = SubscriptionAssignment.model_validate(parent["model_info"][ASSIGNMENT])
    configured = parent["litellm_params"].get("timeout") or router.timeout or 600
    timeout = (
        float(configured)
        if isinstance(configured, (int, float)) and configured > 0
        else 600
    )
    value = Policy(
        parent["model_info"]["id"],
        assignment.revision,
        len(assignment.subscription_ids) - 1,
        time.monotonic() + timeout,
    )
    logging_obj = kwargs.get("litellm_logging_obj")
    if logging_obj is not None:
        logging_obj._subscription_policy = value
    return value


def cooldown_seconds(router, parent, error):
    import litellm

    configured = parent.get("model_info", {}).get("cooldown_time")
    if not isinstance(configured, (int, float)) or configured <= 0:
        configured = router.cooldown_time or 30
    headers = litellm.litellm_core_utils.exception_mapping_utils._get_response_headers(
        original_exception=error
    )
    retry_after = (
        litellm.utils._get_retry_after_from_exception_header(response_headers=headers)
        if headers
        else None
    )
    return max(configured, retry_after or 0)


async def record_failure(router, kwargs, error):
    """Skip failed members for this request; retain native shared cooldown state."""
    logging_obj = kwargs.get("litellm_logging_obj")
    selection = getattr(logging_obj, "_subscription_selection", None)
    from .attribution import snapshot

    attempts = snapshot(logging_obj).get("subscription_attempts", [])
    if (
        not isinstance(selection, Selection)
        or not attempts
        or attempts[-1]["model_id"] != selection.member_id
    ):
        return False
    code = getattr(error, "status_code", None)
    from datetime import datetime, timezone

    from litellm.router_utils.cooldown_handlers import is_caller_timeout_408

    started = logging_obj.model_call_details.get(
        "api_call_start_time"
    ) or logging_obj.model_call_details.get("start_time")
    clock_zone = started.tzinfo if isinstance(started, datetime) else timezone.utc
    if is_caller_timeout_408(
        logging_obj.model_call_details, code, ended=datetime.now(tz=clock_zone)
    ):
        return False
    retryable = code in (401, 403, 408, 429) or (
        isinstance(code, int) and 500 <= code < 600
    )
    if not retryable:
        return False
    cooldown = cooldown_seconds(router, selection.parent, error)
    router.cooldown_cache.add_deployment_to_cooldown(
        model_id=selection.member_id,
        original_exception=error,
        exception_status=code,
        cooldown_time=cooldown,
    )
    if code in (401, 403):
        from .accounts import Accounts

        accounts = Accounts()
        account = next(
            (
                a
                for a in accounts
                if a.status()["subscription_id"] == selection.subscription_id
            ),
            None,
        )
        if account is not None:
            account.mark_provider_rejected(
                getattr(logging_obj, "_subscription_credential_fingerprint", None)
            )
    return True
