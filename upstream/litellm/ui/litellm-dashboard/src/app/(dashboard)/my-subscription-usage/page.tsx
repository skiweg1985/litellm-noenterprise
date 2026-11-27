"use client";
import { useQuery } from "@tanstack/react-query";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Card, CardHeader, CardContent, CardTitle } from "@/components/ui/card";
import { getMySubscriptionUsage, type PersonalAccount } from "@/lib/http/subscriptionFairUse";
import { policyNotice, windowNames, localReset, percent } from "@/components/subscriptions/fair-use/presentation";
import { fairUseKey, polling, type Session } from "@/components/subscriptions/fair-use/queries";

function PersonalUsage({ account }: { account: PersonalAccount }) {
  const notice = policyNotice(account);
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2 className="break-words">{account.display_name}</h2>
        </CardTitle>
        {!notice && account.windows.every((window) => !window.scarce) && <p>No limits are active.</p>}
      </CardHeader>
      <CardContent>
        {notice ? (
          <p>{notice}</p>
        ) : (
          <div className="grid gap-5 md:grid-cols-2">
            {account.windows.map((window) => {
              const reset = localReset(window.reset_at);
              return (
                <section aria-label={windowNames[window.id]} className="min-w-0 space-y-2" key={window.id}>
                  <h3 className="font-semibold">{windowNames[window.id]}</h3>
                  <p>
                    Your share: {percent(window.share)} · Your allowance:{" "}
                    {window.factor === 0 ? "Exempt" : percent(window.allowance)}
                  </p>
                  {window.allowance > 0 && (
                    <meter
                      className="block h-3 w-full"
                      aria-label="Your share of your allowance"
                      aria-valuetext={percent(window.share) + " of " + percent(window.allowance)}
                      min={0}
                      max={window.allowance}
                      value={Math.min(window.allowance, window.share)}
                    />
                  )}
                  {window.throttled ? (
                    <p className="font-medium text-amber-700 dark:text-amber-300">
                      {reset ? "Limited until " + reset : "Limited"}
                    </p>
                  ) : (
                    reset && <p>Resets {reset}</p>
                  )}
                </section>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Usage({ session }: { session: Session }) {
  const query = useQuery({
    queryKey: [...fairUseKey(session), "me"],
    queryFn: ({ signal }) => getMySubscriptionUsage(session.accessToken, signal),
    ...polling,
  });
  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4 sm:p-6">
      <h1 className="text-2xl font-semibold">My subscription usage</h1>
      {query.isPending && <p role="status">Loading your usage…</p>}
      {query.isError ? (
        <p role="alert">Could not load your subscription usage.</p>
      ) : (
        query.data &&
        (query.data.accounts.length ? (
          query.data.accounts.map((account, i) => <PersonalUsage key={account.display_name + i} account={account} />)
        ) : (
          <p>No subscriptions available to you.</p>
        ))
      )}
    </div>
  );
}
export default function MySubscriptionUsagePage() {
  const { userId, accessToken, isLoading, isAuthorized } = useAuthorized();
  if (isLoading)
    return (
      <p role="status" className="p-6">
        Checking your session…
      </p>
    );
  if (!isAuthorized || !accessToken)
    return (
      <p role="alert" className="p-6">
        Sign in to view your subscription usage.
      </p>
    );
  return <Usage session={{ userId, accessToken }} />;
}
