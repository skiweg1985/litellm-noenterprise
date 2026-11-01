"use client";

import { Button } from "@/components/ui/button";
import { useSubscriptionLogRouting } from "../request/subscriptionLogRouting";
import { useLogDetailRouting } from "../request/logDetailRouting";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { subscriptionAttempts, subscriptionAttributionLabel } from "../subscriptionAttribution";

const OUTCOMES: Record<string, string> = {
  success: "Success",
  failure: "Failure",
  cancelled: "Cancelled",
  incomplete: "Incomplete",
};
const REASONS: Record<string, string> = { initial: "Initial attempt", retry: "Retry", fallback: "Fallback" };

export function SubscriptionAttempts({
  metadata,
  provider,
  model,
}: {
  metadata: Record<string, unknown>;
  provider?: string;
  model: string;
}) {
  const [, setSubscriptionRoute] = useSubscriptionLogRouting();
  const { close } = useLogDetailRouting();
  const filterSubscription = (subscriptionId: string) => {
    void setSubscriptionRoute({ subscription_id: subscriptionId });
    close();
  };
  const attempts = subscriptionAttempts(metadata);
  const label = subscriptionAttributionLabel({ metadata, custom_llm_provider: provider, model });
  if (!attempts.length && label === "—") return null;
  return (
    <Card size="sm" className="mb-6">
      <CardHeader>
        <CardTitle>Subscription attempts</CardTitle>
      </CardHeader>
      <CardContent>
        {attempts.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {label === "No provider attempt"
              ? "No subscription provider attempt was recorded for this request."
              : "Unknown/legacy: this log has no recorded subscription attribution."}
          </p>
        ) : (
          <ol aria-label="Subscription attempts" className="space-y-3">
            {attempts.map((attempt) => (
              <li key={attempt.attempt_id} className="min-w-0 rounded-md border p-3 text-sm">
                <div className="flex flex-wrap justify-between gap-2">
                  <span className="font-medium break-all">
                    {attempt.sequence}. <span>{attempt.display_name}</span>
                  </span>
                  <span>
                    {OUTCOMES[attempt.outcome] ?? "Unknown"} · {REASONS[attempt.reason] ?? "Unknown reason"}
                  </span>
                </div>
                <dl className="mt-2 grid grid-cols-1 gap-1 break-all sm:grid-cols-[9rem_1fr]">
                  <dt className="text-muted-foreground">Subscription ID</dt>
                  <dd>{attempt.subscription_id}</dd>
                  <dt className="text-muted-foreground">Connection revision</dt>
                  <dd>{attempt.connection_revision ?? "Unknown"}</dd>
                  <dt className="text-muted-foreground">Upstream model</dt>
                  <dd>{attempt.model ?? "Unknown"}</dd>
                  <dt className="text-muted-foreground">Deployment ID</dt>
                  <dd>{attempt.model_id ?? "Unknown"}</dd>
                  <dt className="text-muted-foreground">Input / output tokens</dt>
                  <dd>
                    {attempt.input_tokens ?? "Unknown"} / {attempt.output_tokens ?? "Unknown"}
                  </dd>
                  <dt className="text-muted-foreground">Started / ended</dt>
                  <dd>
                    {attempt.started_at ?? "Unknown"} / {attempt.ended_at ?? "Unknown"}
                  </dd>
                </dl>
                <Button
                  className="mt-3"
                  size="sm"
                  variant="outline"
                  onClick={() => filterSubscription(attempt.subscription_id)}
                  aria-label={`Filter logs for ${attempt.display_name}`}
                >
                  Filter logs for this subscription
                </Button>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}
