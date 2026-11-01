"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { subscriptionLogsLink, useSubscriptionLogRouting } from "@/components/logs/request/subscriptionLogRouting";
import { getObservedUsage, type SubscriptionUsage, type UsageRange } from "@/lib/http/subscriptionUsage";

function recorded(value: number | null | undefined, unknown: number | undefined, direction: string) {
  const amount = value == null ? "Unknown" : `${value.toLocaleString()} recorded`;
  return unknown
    ? `${amount} · ${unknown} ${unknown === 1 ? "attempt" : "attempts"} without ${direction} usage`
    : amount;
}

function UsageCard({ item, range }: { item: SubscriptionUsage; range: UsageRange }) {
  const metrics = [
    ["Logical requests", item.logical_requests],
    ["Provider attempts", item.provider_attempts],
    ["Failed attempts", item.failed_attempts],
    ["Reserve switches", item.reserve_switches],
    ["Input tokens", recorded(item.input_tokens, item.unknown_input_attempts, "input")],
    ["Output tokens", recorded(item.output_tokens, item.unknown_output_attempts, "output")],
  ];
  const selected = { ...range, subscription_id: item.subscription_id };
  let status = "Subscription";
  if (item.retired === null) status = "Historical subscription";
  if (item.retired) status = "Retired subscription";
  return (
    <Card role="region" aria-label={`Usage for ${item.display_name}`}>
      <CardHeader className="space-y-2">
        <CardTitle>
          <h2 className="break-words">{item.display_name}</h2>
        </CardTitle>
        <p className="text-sm text-muted-foreground">{status}</p>
        <div className="flex flex-wrap gap-4 text-sm">
          <Link
            className="text-primary underline"
            href={subscriptionLogsLink(uiHref("chatgpt-subscriptions/usage/"), selected)}
            aria-label={`View usage for ${item.display_name}`}
          >
            Only this subscription
          </Link>
          <Link
            className="text-primary underline"
            href={subscriptionLogsLink(uiHref("logs/"), selected)}
            aria-label={`View logs for ${item.display_name}`}
          >
            View logs
          </Link>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {metrics.map(([name, value]) => (
            <div key={name} className="min-w-0">
              <dt className="text-sm text-muted-foreground">{name}</dt>
              <dd className="break-words font-medium">{value ?? 0}</dd>
            </div>
          ))}
        </dl>
        {!!item.historical_names?.length && (
          <p className="break-words text-sm">Names in retained logs: {item.historical_names.join(", ")}</p>
        )}
        <details className="space-y-2 text-sm">
          <summary className="cursor-pointer font-medium">Connection history</summary>
          {item.revisions?.length ? (
            item.revisions.map((revision) => (
              <div key={revision.connection_revision ?? "unknown"} className="rounded border p-3">
                <p className="break-all">{revision.connection_revision ?? "Unknown connection revision"}</p>
                <p>
                  {revision.logical_requests} requests · {revision.provider_attempts} attempts ·{" "}
                  {revision.failed_attempts} failures · {revision.reserve_switches} reserve switches
                </p>
                <p>Input: {recorded(revision.input_tokens, revision.unknown_input_attempts, "input")}</p>
                <p>Output: {recorded(revision.output_tokens, revision.unknown_output_attempts, "output")}</p>
              </div>
            ))
          ) : (
            <p>No recorded attempts in this period.</p>
          )}
        </details>
      </CardContent>
    </Card>
  );
}

function Usage({ accessToken, userId }: { accessToken: string; userId: string | null }) {
  const [route, setRoute] = useSubscriptionLogRouting();
  const [defaults] = useState(() => {
    const end = new Date();
    end.setMilliseconds(0);
    return { start_date: new Date(end.getTime() - 86400000).toISOString(), end_date: end.toISOString() };
  });
  const range: UsageRange = {
    start_date: route.start_date ?? defaults.start_date,
    end_date: route.end_date ?? defaults.end_date,
    subscription_id: route.subscription_id,
  };
  const [draft, setDraft] = useState({ start: range.start_date.slice(0, 19), end: range.end_date.slice(0, 19) });
  const [invalid, setInvalid] = useState(false);
  const query = useQuery({
    queryKey: ["subscription-observed-usage", userId, range],
    queryFn: ({ signal }) => getObservedUsage(accessToken, range, signal),
    retry: false,
  });
  const data = query.isError ? undefined : query.data;
  function applyPeriod(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const start = Date.parse(draft.start + "Z"),
      end = Date.parse(draft.end + "Z");
    if (!Number.isFinite(start) || !Number.isFinite(end) || start > end) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    void setRoute({ start_date: new Date(start).toISOString(), end_date: new Date(end).toISOString() });
  }
  return (
    <div className="mx-auto max-w-7xl space-y-6 p-4 sm:p-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Observed subscription usage</h1>
        <Link className="text-primary underline" href={uiHref("chatgpt-subscriptions/")}>
          Manage subscriptions and provider quota
        </Link>
      </header>
      <p className="text-sm text-muted-foreground">
        These are locally recorded requests and provider attempts, separate from provider quota and reset windows. No
        subscription fee is converted into a per-request charge. One request can use multiple subscriptions, so their
        request counts must not be added together.
      </p>
      <form className="flex flex-wrap items-end gap-3" onSubmit={applyPeriod}>
        <label className="min-w-0 space-y-1 text-sm">
          From (UTC)
          <Input
            aria-label="From (UTC)"
            type="datetime-local"
            step="1"
            value={draft.start}
            onChange={(event) => setDraft({ ...draft, start: event.target.value })}
            required
          />
        </label>
        <label className="min-w-0 space-y-1 text-sm">
          Through (UTC, inclusive)
          <Input
            aria-label="Through (UTC, inclusive)"
            type="datetime-local"
            step="1"
            value={draft.end}
            onChange={(event) => setDraft({ ...draft, end: event.target.value })}
            required
          />
        </label>
        <Button type="submit">Apply period</Button>
        <Button variant="outline" type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>
          Refresh usage
        </Button>
        {route.subscription_id && (
          <Button variant="outline" type="button" onClick={() => void setRoute({ subscription_id: null })}>
            All subscriptions
          </Button>
        )}
      </form>
      {invalid && <p role="alert">Choose a valid period whose start is before its end.</p>}
      {query.isPending && <p role="status">Loading observed usage…</p>}
      {query.isError && (
        <p role="alert">
          Observed usage is unavailable or your session no longer has administrator access. Retry with Refresh usage.
        </p>
      )}
      {data && (
        <>
          <section aria-label="History coverage" className="space-y-2 rounded border p-4 text-sm">
            <p>
              Only retained request logs are included. Complete historical coverage is unknown; older or deleted logs
              cannot be reconstructed.
            </p>
            <p>
              Oldest retained log:{" "}
              {data.coverage.earliest_retained_log_at
                ? new Date(data.coverage.earliest_retained_log_at).toISOString()
                : "Unknown — no retained logs"}
              .
            </p>
            <p>
              {data.coverage.unattributed_log_records} unattributed log records from ChatGPT in this period. They are
              not assigned to any subscription.
            </p>
            <p>
              {data.coverage.unknown_provider_log_records} records have neither a provider nor subscription attribution;
              their source is unknown.
            </p>
            <p>
              {data.coverage.cache_hit_log_records} ChatGPT cache-hit log records are excluded from provider usage.
              Token totals include recorded values only; missing usage stays unknown.
            </p>
          </section>
          {data.subscriptions.length ? (
            data.subscriptions.map((item) => (
              <UsageCard
                key={item.subscription_id}
                item={item}
                range={{ start_date: data.start_date, end_date: data.end_date }}
              />
            ))
          ) : (
            <p>No subscriptions or attributed attempts in this period.</p>
          )}
        </>
      )}
    </div>
  );
}

export default function SubscriptionUsagePage() {
  const { accessToken, userId, userRole, isLoading, isAuthorized } = useAuthorized();
  if (isLoading)
    return (
      <p className="p-6" role="status">
        Checking your session…
      </p>
    );
  if (!isAuthorized || !accessToken || !isProxyAdminRole(userRole ?? ""))
    return (
      <p className="p-6" role="alert">
        Proxy administrator access is required to view subscription usage.
      </p>
    );
  return <Usage key={`${userId}:${accessToken}`} accessToken={accessToken} userId={userId} />;
}
