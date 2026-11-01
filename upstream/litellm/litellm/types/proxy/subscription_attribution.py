"""Safe persisted subscription-attempt fields exposed inside spend-log metadata."""

from typing import Literal
from typing_extensions import NotRequired, TypedDict


class SubscriptionAttemptRecord(TypedDict):
    attempt_id: str
    sequence: int
    subscription_id: str
    display_name: str
    connection_revision: str | None
    model: str | None
    model_id: str | None
    parent_model_id: NotRequired[str | None]
    started_at: str | None
    ended_at: str | None
    outcome: Literal["success", "failure", "cancelled", "incomplete"]
    reason: Literal["initial", "retry", "fallback"]
    routing_mode: NotRequired[Literal["fixed", "priority", "distribution"] | None]
    reserve_switch: NotRequired[bool]
    input_tokens: int | None
    output_tokens: int | None
    error_code: NotRequired[str]


class SubscriptionAttributionMetadata(TypedDict, total=False):
    subscription_logical_id: str
    subscription_ids: list[str]
    subscription_attempts: list[SubscriptionAttemptRecord]
