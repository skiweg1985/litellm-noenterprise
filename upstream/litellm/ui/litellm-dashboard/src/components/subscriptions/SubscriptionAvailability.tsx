"use client";

import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { getSubscriptionAvailability } from "@/lib/http/chatgptSubscriptions";

const descriptions = {
  eligible: "Eligible by connection and cooldown",
  cooldown: "Cooling down; retry after the provider limit or outage clears",
  disconnected: "Reconnect this subscription",
  retired: "Retired; select another subscription",
  credential_rejected: "Sign-in rejected; reconnect this subscription",
  missing: "Subscription is missing; update the assignment",
};

const summaries = {
  paused: "This model is paused. Resume it before sending requests.",
  unavailable:
    "No assigned subscription is currently eligible. Wait for cooldowns to expire or reconnect an unavailable subscription.",
  eligible:
    "At least one assigned subscription is eligible by connection and cooldown. Request limits are checked when used.",
};

export function SubscriptionAvailability({
  accessToken,
  modelId,
  names,
}: {
  accessToken: string | null;
  modelId: string;
  names: Record<string, string>;
}) {
  const options = {
    queryKey: ["subscription-model-availability", modelId, accessToken],
    queryFn: ({ signal }: { signal: AbortSignal }) => getSubscriptionAvailability(accessToken!, modelId, signal),
    enabled: Boolean(accessToken),
    retry: false,
    refetchInterval: 15_000,
  };
  const query = useQuery(options);
  const value = query.isError ? undefined : query.data;
  return (
    <section aria-label="Saved model routing availability" className="space-y-2 text-sm">
      <h3 className="font-medium">Saved model routing availability</h3>
      {query.isError && <p role="alert">Routing availability could not be checked. Refresh to try again.</p>}
      {query.isPending && <p>Checking routing availability…</p>}
      {value && (
        <>
          <p role="status">{summaries[value.state]}</p>
          <ul className="space-y-1">
            {value.members.map((member) => (
              <li key={member.subscription_id}>
                {names[member.subscription_id] || "Assigned subscription"}: {descriptions[member.state]}
                {member.retry_at != null && <> (retry after {new Date(member.retry_at * 1000).toLocaleTimeString()})</>}
              </li>
            ))}
          </ul>
          <p className="text-muted-foreground">
            {value.cooldown_scope === "worker"
              ? "This worker only: cooldowns can differ on other workers without a shared Redis cache."
              : "Cooldowns use the configured shared Redis cache."}{" "}
            Connection and retirement changes apply across workers.
          </p>
          <p className="text-muted-foreground">Applies to the saved assignment; unsaved changes are not included.</p>
        </>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={query.isFetching}
        onClick={() => void query.refetch()}
      >
        Refresh routing availability
      </Button>
    </section>
  );
}
