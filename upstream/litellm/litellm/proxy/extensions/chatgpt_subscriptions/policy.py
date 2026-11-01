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
    retryable = code in (401, 403, 408, 429) or (
        isinstance(code, int) and 500 <= code < 600
    )
    if not retryable:
        return False
    cooldown = selection.parent.get("model_info", {}).get("cooldown_time")
    if not isinstance(cooldown, (int, float)) or cooldown <= 0:
        cooldown = router.cooldown_time or 30
    headers = getattr(getattr(error, "response", None), "headers", None) or getattr(
        error, "litellm_response_headers", None
    )
    if headers:
        import litellm

        cooldown = max(
            cooldown,
            litellm.calculate_retry_after(
                remaining_retries=0,
                max_retries=1,
                response_headers=headers,
                min_timeout=0,
            ),
        )
    router.cooldown_cache.add_deployment_to_cooldown(
        model_id=selection.member_id,
        original_exception=error,
        exception_status=code,
        cooldown_time=cooldown,
    )
    if code in (401, 403):
        from . import tokens
        from .accounts import Accounts, _fingerprint

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
            with (
                tokens.locked(accounts.registry.lock_path),
                tokens.locked(account.path),
            ):
                record = tokens.read(account.path) or {}
                fingerprint = _fingerprint(record.get("access_token"))
                if fingerprint and fingerprint == getattr(
                    logging_obj, "_subscription_credential_fingerprint", None
                ):
                    account._update(provider_rejected_fingerprint=fingerprint)
    return True
