"""Trusted, request-local subscription attempts; persisted with ordinary spend logs."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from litellm.types.proxy.subscription_attribution import (
    SubscriptionAttemptRecord,
    SubscriptionAttributionMetadata,
)

FIELDS = frozenset(
    {"subscription_logical_id", "subscription_ids", "subscription_attempts"}
)


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CredentialSnapshot:
    subscription_id: str
    display_name: str
    connection_revision: str | None
    credential_fingerprint: str | None = field(default=None, repr=False, compare=False)


@dataclass
class Attempt:
    value: SubscriptionAttemptRecord

    def finish(self, outcome, response=None, error=None):
        if self.value.get("ended_at") is not None:
            return
        usage = getattr(response, "usage", None)
        if isinstance(response, dict):
            usage = response.get("usage")
        if usage is not None and hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        self.value.update(outcome=outcome, ended_at=now())
        if isinstance(usage, dict):
            for target, keys in [
                ("input_tokens", ("input_tokens", "prompt_tokens")),
                ("output_tokens", ("output_tokens", "completion_tokens")),
            ]:
                value = next(
                    (usage[key] for key in keys if isinstance(usage.get(key), int)),
                    None,
                )
                self.value[target] = value if value is not None and value >= 0 else None
        code = getattr(error, "status_code", None)
        if isinstance(code, int):
            self.value["error_code"] = str(code)


@dataclass
class Ledger:
    logical_id: str = field(default_factory=lambda: str(uuid4()))
    attempts: list[Attempt] = field(default_factory=list)


def ledger(logging_obj):
    value = getattr(logging_obj, "_chatgpt_subscription_ledger", None)
    if not isinstance(value, Ledger):
        value = Ledger()
        logging_obj._chatgpt_subscription_ledger = value
    return value


def begin(logging_obj, credential, model):
    if not isinstance(credential, CredentialSnapshot):
        return None
    state = ledger(logging_obj)
    logging_obj._subscription_credential_fingerprint = credential.credential_fingerprint
    selection = getattr(logging_obj, "_subscription_selection", None)
    routing_mode = (
        selection.parent.get("model_info", {})
        .get("subscription_assignment", {})
        .get("mode")
        if selection is not None
        else None
    )
    details = logging_obj.model_call_details
    params = details.get("litellm_params") or {}
    metadata = params.get("litellm_metadata") or params.get("metadata") or {}
    model_id = getattr(logging_obj, "_chatgpt_subscription_deployment_id", None)
    if not isinstance(model_id, str):
        model_id = (metadata.get("model_info") or {}).get("id")
    previous = state.attempts[-1].value if state.attempts else None
    reason = (
        "initial"
        if previous is None
        else (
            "fallback"
            if routing_mode != "distribution"
            and previous["subscription_id"] != credential.subscription_id
            else "retry"
        )
    )
    value: SubscriptionAttemptRecord = {
        "attempt_id": str(uuid4()),
        "sequence": len(state.attempts) + 1,
        "subscription_id": credential.subscription_id,
        "display_name": credential.display_name,
        "connection_revision": credential.connection_revision,
        "model": model,
        "model_id": model_id,
        "parent_model_id": getattr(logging_obj, "_subscription_parent_model_id", None),
        "started_at": now(),
        "ended_at": None,
        "outcome": "incomplete",
        "reason": reason,
        "routing_mode": routing_mode,
        "reserve_switch": routing_mode == "priority" and reason == "fallback",
        "input_tokens": None,
        "output_tokens": None,
    }
    attempt = Attempt(value)
    state.attempts.append(attempt)
    return attempt


def snapshot(logging_obj) -> SubscriptionAttributionMetadata:
    state = getattr(logging_obj, "_chatgpt_subscription_ledger", None)
    if not isinstance(state, Ledger) or not state.attempts:
        return {}
    attempts = [dict(attempt.value) for attempt in state.attempts]
    return {
        "subscription_logical_id": state.logical_id,
        "subscription_ids": list(dict.fromkeys(a["subscription_id"] for a in attempts)),
        "subscription_attempts": attempts,
    }


def stamp(logging_obj):
    """Replace reserved values with trusted state before success/failure serialization."""
    details = getattr(logging_obj, "model_call_details", None)
    if not isinstance(details, dict):
        return {}
    fields = snapshot(logging_obj)
    params = details.setdefault("litellm_params", {})
    for owner, key in (
        (params, "metadata"),
        (params, "litellm_metadata"),
        (details, "metadata"),
    ):
        bucket = owner.get(key)
        if isinstance(bucket, dict):
            for name in FIELDS:
                bucket.pop(name, None)
            bucket.update(fields)
    return fields


def cancel(logging_obj):
    state = getattr(logging_obj, "_chatgpt_subscription_ledger", None)
    if isinstance(state, Ledger):
        for attempt in state.attempts:
            attempt.finish("cancelled")
    stamp(logging_obj)
