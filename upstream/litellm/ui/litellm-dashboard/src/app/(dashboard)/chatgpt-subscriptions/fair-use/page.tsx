"use client";
import Link from "next/link";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TableCell, TableRow } from "@/components/ui/table";
import { FairUseTable } from "@/components/subscriptions/fair-use/FairUseTable";
import { FactorsEditor } from "@/components/subscriptions/fair-use/FactorsEditor";
import { ModelWeightsEditor } from "@/components/subscriptions/fair-use/ModelWeightsEditor";
import { useDirectoryNames, useFairUseStatus, type Session } from "@/components/subscriptions/fair-use/queries";
import { policyNotice, windowTitle, localReset, percent } from "@/components/subscriptions/fair-use/presentation";
import type { AdminAccount } from "@/lib/http/subscriptionFairUse";

function SubscriptionStatus({ account, names }: { account: AdminAccount; names: Map<string, string> }) {
  const notice = policyNotice(account);
  return (
    <Card aria-label={account.display_name} role="region" className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="break-words">{account.display_name}</h3>
        </CardTitle>
        {notice && <p>{notice}</p>}
      </CardHeader>
      <CardContent className="grid min-w-0 gap-5 xl:grid-cols-2">
        {account.windows.map((window) => {
          const reset = localReset(window.reset_at);
          return (
            <section key={window.id} aria-label={windowTitle(window)} className="min-w-0 space-y-2">
              <h4 className="font-semibold">{windowTitle(window)}</h4>
              <p>
                {window.used_percent == null ? "Usage unavailable" : percent(window.used_percent) + " used"} · limits
                from {percent(window.threshold)}
              </p>
              {!notice && window.scarce && (
                <strong className="inline-block rounded bg-amber-100 px-2 text-amber-900">Limited</strong>
              )}
              {reset && <p>Resets {reset}</p>}
              {window.users.length ? (
                <FairUseTable
                  name={windowTitle(window) + " users"}
                  columns={["User", "Share", "Allowance", "Factor", "Status"]}
                >
                  {window.users.map((user) => (
                    <TableRow key={user.user_id}>
                      <TableCell>{names.get(user.user_id) ?? user.user_id}</TableCell>
                      <TableCell>{percent(user.share)}</TableCell>
                      <TableCell>{percent(user.allowance)}</TableCell>
                      <TableCell>{user.factor === 0 ? "Exempt" : user.factor}</TableCell>
                      <TableCell>{user.throttled ? "Limited" : "Available"}</TableCell>
                    </TableRow>
                  ))}
                </FairUseTable>
              ) : (
                account.quota_state === "current" && <p className="text-muted-foreground">No usage this window.</p>
              )}
            </section>
          );
        })}
      </CardContent>
    </Card>
  );
}

function FairUseDashboard({ session }: { session: Session }) {
  const status = useFairUseStatus(session);
  const accounts = status.isError ? [] : status.data?.accounts ?? [];
  const names = useDirectoryNames(
    session,
    accounts.flatMap((account) => account.windows.flatMap((window) => window.users.map((user) => user.user_id))),
  );
  return (
    <div className="mx-auto max-w-7xl space-y-8 p-4 sm:p-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Fair use</h1>
        <Link className="text-primary underline" href={uiHref("chatgpt-subscriptions/")}>
          ChatGPT subscriptions
        </Link>
      </header>
      <section aria-labelledby="status-heading" className="space-y-3">
        <h2 id="status-heading" className="text-xl font-semibold">
          Subscription status
        </h2>
        {status.isPending && <p role="status">Loading subscription status…</p>}
        {status.isError && <p role="alert">Could not load subscription status.</p>}
        {status.isSuccess && !accounts.length && <p>No subscriptions yet.</p>}
        {accounts.map((account) => (
          <SubscriptionStatus key={account.subscription_id} account={account} names={names.users} />
        ))}
      </section>
      <FactorsEditor
        session={session}
        unknown={{
          teams: new Set(accounts.flatMap((account) => account.unknown_ids.teams ?? [])),
          users: new Set(accounts.flatMap((account) => account.unknown_ids.users ?? [])),
        }}
      />
      <ModelWeightsEditor session={session} />
    </div>
  );
}
export default function FairUsePage() {
  const { userId, accessToken, userRole, isViewOnly, isLoading, isAuthorized } = useAuthorized();
  if (isLoading)
    return (
      <p role="status" className="p-6">
        Checking your session…
      </p>
    );
  const canManage = !isViewOnly && isProxyAdminRole(userRole ?? "");
  if (!isAuthorized || !accessToken || !canManage)
    return (
      <p role="alert" className="p-6">
        Proxy administrator access is required to manage fair use.
      </p>
    );
  return <FairUseDashboard key={JSON.stringify([userId, accessToken])} session={{ accessToken, userId }} />;
}
