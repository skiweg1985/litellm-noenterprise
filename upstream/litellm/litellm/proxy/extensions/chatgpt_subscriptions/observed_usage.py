"""Aggregate retained, dispatch-owned subscription attempts without provider calls."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .admin import litellm_admin

API = "/chatgpt/api/observed-usage"


class UsageMetrics(BaseModel):
    logical_requests: int = 0
    provider_attempts: int = 0
    failed_attempts: int = 0
    reserve_switches: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    unknown_input_attempts: int = 0
    unknown_output_attempts: int = 0


class RevisionUsage(UsageMetrics):
    connection_revision: str | None


class SubscriptionUsage(UsageMetrics):
    subscription_id: str
    display_name: str
    historical_names: list[str] = Field(default_factory=list)
    retired: bool | None = None
    revisions: list[RevisionUsage] = Field(default_factory=list)


class UsageCoverage(BaseModel):
    earliest_retained_log_at: datetime | None = None
    unattributed_log_records: int = 0
    unknown_provider_log_records: int = 0
    cache_hit_log_records: int = 0
    history_complete: bool = False


class ObservedUsage(BaseModel):
    start_date: str
    end_date: str
    subscriptions: list[SubscriptionUsage]
    coverage: UsageCoverage


# Select the same inclusive UTC startTime window as /spend/logs/ui before
# expanding attempts. No mutable model assignments or names supply attribution.
AGGREGATE = """
WITH scoped AS (
    SELECT request_id, metadata, updated_at FROM "LiteLLM_SpendLogs"
    WHERE "startTime" >= $1::timestamptz AND "startTime" <= $2::timestamptz
      AND COALESCE(LOWER(cache_hit), '') != 'true'
), expanded AS (
    SELECT request_id, updated_at, metadata->>'subscription_logical_id' AS logical_id, a
    FROM scoped CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(metadata->'subscription_attempts') = 'array'
             THEN metadata->'subscription_attempts' ELSE '[]'::jsonb END
    ) a
    WHERE COALESCE(metadata->>'subscription_logical_id', '') != ''
      AND COALESCE(a->>'attempt_id', '') != ''
      AND COALESCE(a->>'subscription_id', '') != ''
), attempts AS (
    SELECT DISTINCT ON (logical_id, a->>'attempt_id') logical_id, a
    FROM expanded
    ORDER BY logical_id, a->>'attempt_id',
        (COALESCE(a->>'outcome', '') IN ('success', 'failure', 'cancelled')) DESC,
        a->>'ended_at' DESC NULLS LAST, updated_at DESC, request_id DESC
), measured AS (
    SELECT logical_id, a, a->>'subscription_id' AS subscription_id,
        a->>'connection_revision' AS connection_revision,
        CASE WHEN jsonb_typeof(a->'input_tokens') = 'number' AND a->>'input_tokens' ~ '^[0-9]+$'
             THEN (a->>'input_tokens')::numeric END AS input_tokens,
        CASE WHEN jsonb_typeof(a->'output_tokens') = 'number' AND a->>'output_tokens' ~ '^[0-9]+$'
             THEN (a->>'output_tokens')::numeric END AS output_tokens
    FROM attempts WHERE ($3::text IS NULL OR a->>'subscription_id' = $3)
)
SELECT subscription_id, connection_revision, GROUPING(connection_revision)::int AS is_total,
    COUNT(DISTINCT logical_id)::int AS logical_requests, COUNT(*)::int AS provider_attempts,
    COUNT(*) FILTER (WHERE a->>'outcome' = 'failure')::int AS failed_attempts,
    COUNT(*) FILTER (WHERE a->'reserve_switch' = 'true'::jsonb)::int AS reserve_switches,
    SUM(input_tokens)::bigint AS input_tokens, SUM(output_tokens)::bigint AS output_tokens,
    COUNT(*) FILTER (WHERE input_tokens IS NULL)::int AS unknown_input_attempts,
    COUNT(*) FILTER (WHERE output_tokens IS NULL)::int AS unknown_output_attempts,
    COALESCE(jsonb_agg(DISTINCT a->>'display_name') FILTER (WHERE a->>'display_name' IS NOT NULL), '[]'::jsonb) AS historical_names
FROM measured GROUP BY GROUPING SETS ((subscription_id), (subscription_id, connection_revision))
ORDER BY subscription_id, is_total DESC, connection_revision
"""

COVERAGE = """
SELECT (SELECT MIN("startTime") FROM "LiteLLM_SpendLogs") AS earliest_retained_log_at,
    COUNT(*) FILTER (WHERE custom_llm_provider = 'chatgpt' AND COALESCE(LOWER(cache_hit), '') = 'true')::int AS cache_hit_log_records,
    COUNT(*) FILTER (WHERE COALESCE(custom_llm_provider, '') = '' AND COALESCE(LOWER(cache_hit), '') != 'true' AND COALESCE(metadata->>'subscription_logical_id', '') = '')::int AS unknown_provider_log_records,
    COUNT(*) FILTER (WHERE custom_llm_provider = 'chatgpt' AND COALESCE(LOWER(cache_hit), '') != 'true' AND
        (COALESCE(metadata->>'subscription_logical_id', '') = '' OR
         COALESCE(jsonb_array_length(CASE WHEN jsonb_typeof(metadata->'subscription_attempts') = 'array'
             THEN metadata->'subscription_attempts' ELSE '[]'::jsonb END), 0) = 0))::int AS unattributed_log_records
FROM "LiteLLM_SpendLogs" WHERE "startTime" >= $1::timestamptz AND "startTime" <= $2::timestamptz
"""


def utc_second(value: datetime) -> str:
    if value.tzinfo is None:
        raise HTTPException(400, "Choose a time range with an explicit timezone.")
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def router(accounts, dependency=None):
    api = APIRouter(
        dependencies=[Depends(dependency or litellm_admin())],
        tags=["ChatGPT observed usage"],
    )

    @api.get(API, response_model=ObservedUsage)
    async def observed_usage(
        start_date: datetime,
        end_date: datetime,
        response: Response,
        subscription_id: UUID | None = None,
    ):
        start, end = utc_second(start_date), utc_second(end_date)
        if start > end:
            raise HTTPException(
                400, "The start of the time range must not follow its end."
            )
        from litellm.proxy.proxy_server import prisma_client

        if prisma_client is None:
            raise HTTPException(503, "Request history is unavailable. Retry later.")
        identifier = str(subscription_id) if subscription_id else None
        rows = await prisma_client.db.query_raw(AGGREGATE, start, end, identifier)
        coverage = (await prisma_client.db.query_raw(COVERAGE, start, end))[0]
        statuses = await asyncio.to_thread(
            lambda: [account.status() for account in accounts]
        )
        subscriptions = {
            item["subscription_id"]: SubscriptionUsage(
                subscription_id=item["subscription_id"],
                display_name=item["display_name"],
                retired=item["retired"],
            )
            for item in statuses
            if identifier is None or item["subscription_id"] == identifier
        }
        for row in rows:
            key = row["subscription_id"]
            names = sorted(row["historical_names"])
            item = subscriptions.setdefault(
                key,
                SubscriptionUsage(
                    subscription_id=key,
                    display_name=names[-1] if names else "Historical subscription",
                ),
            )
            metrics = {field: row[field] for field in UsageMetrics.model_fields}
            if row["is_total"]:
                for field, value in metrics.items():
                    setattr(item, field, value)
                item.historical_names = names
            else:
                item.revisions.append(
                    RevisionUsage(
                        connection_revision=row["connection_revision"], **metrics
                    )
                )
        response.headers["Cache-Control"] = "no-store"
        return ObservedUsage(
            start_date=start,
            end_date=end,
            subscriptions=sorted(
                subscriptions.values(),
                key=lambda item: (item.display_name.casefold(), item.subscription_id),
            ),
            coverage=UsageCoverage(**coverage),
        )

    return api
