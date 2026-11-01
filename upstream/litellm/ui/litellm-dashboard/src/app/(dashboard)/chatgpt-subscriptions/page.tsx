"use client";

import Link from "next/link";
import { subscriptionModelHref } from "@/components/subscriptions/modelPrefill";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient, UseMutationOptions, UseQueryOptions } from "@tanstack/react-query";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { ApiError } from "@/lib/http/client";
import {
  getSubscriptions,
  createSubscription,
  renameSubscription,
  updateSubscription,
  SubscriptionAccount,
  SubscriptionAction,
  SubscriptionIdentity,
  QuotaWindow,
  SubscriptionOverview,
} from "@/lib/http/chatgptSubscriptions";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const STATES: Record<string, string> = {
  ok: "Signed in",
  login_pending: "Sign-in in progress",
  token_expired: "Session expired",
  login_required: "Sign-in required",
  logged_out: "Disconnected",
  retired: "Retired",
};
const ERRORS: Record<string, string> = {
  unreachable: "The sign-in provider is unavailable. Try again later.",
  refresh_rejected: "The provider rejected renewal. Sign in again.",
  wrong_account: "The sign-in used a different account and was discarded.",
  duplicate_account: "This account is already assigned to another subscription.",
  login_failed: "Sign-in failed. Try again.",
  no_refresh_token: "This session cannot be renewed. Sign in again.",
  busy: "The account is busy. Try again shortly.",
};

function subscriptionName(account: SubscriptionAccount) {
  return account.display_name || account.account;
}

function date(seconds?: number | null) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "—";
}

function workspace(identity?: SubscriptionIdentity | null) {
  return identity?.account_id ? `${identity.plan || "Workspace"} · ${identity.account_id}` : "—";
}

function verificationLink(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.username || url.password) return null;
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

function WindowQuota({ window }: { window: QuotaWindow }) {
  const remaining = window.remaining_percent;
  const duration = window.window_seconds;
  let label = "Window";
  if (duration) label = duration % 86400 === 0 ? `${duration / 86400} days` : `${Math.round(duration / 3600)} hours`;
  return (
    <div className="space-y-1">
      <p className="flex flex-wrap justify-between gap-2 text-sm">
        <span>{label}</span>
        <span>{remaining == null ? "Unknown" : `${Math.round(remaining)}% remaining`}</span>
      </p>
      {remaining != null && (
        <progress
          className="h-2 w-full"
          max={100}
          value={Math.max(0, Math.min(100, remaining))}
          aria-label={`${label} quota remaining`}
        />
      )}
      <p className="text-xs text-muted-foreground">Resets {date(window.reset_at)}</p>
    </div>
  );
}

type ActionRequest = {
  account: SubscriptionAccount;
  steps: SubscriptionAction[];
  title?: string;
  description?: string;
};

type RequestAction = (steps: SubscriptionAction[], title?: string, description?: string) => void;

function DeviceSignIn({ account, busy }: { account: SubscriptionAccount; busy: boolean }) {
  const verification = account.login && verificationLink(account.login.verification_url);
  const [copyState, setCopyState] = useState("");
  return (
    <>
      {" "}
      {account.state === "login_pending" && account.login && (
        <section className="space-y-2 rounded-lg border p-4" aria-label="Device sign-in">
          <p>
            Enter this code at the provider to sign in to <strong>{subscriptionName(account)}</strong>.
          </p>
          <p className="break-all font-mono text-xl font-semibold" aria-label="Sign-in code">
            {account.login.user_code}
          </p>
          {verification ? (
            <a className="text-primary underline" href={verification} target="_blank" rel="noopener noreferrer">
              Open sign-in page
            </a>
          ) : (
            <p role="alert">The provider did not return a secure sign-in URL.</p>
          )}
          <div>
            <Button
              variant="outline"
              disabled={busy}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(account.login!.user_code);
                  setCopyState("Code copied");
                } catch {
                  setCopyState("Copy unavailable. Select and copy the code above.");
                }
              }}
            >
              Copy code
            </Button>
            <span className="ml-2 text-sm" role="status">
              {copyState}
            </span>
          </div>
          <p className="text-sm text-muted-foreground">
            Expires {date(account.login.expires_at)}
            {account.auto_login ? ". A new code is requested automatically after expiry." : ""}
          </p>
          {account.known_account?.email && (
            <p className="break-words text-sm">
              Expected account: {account.known_account.email} · {workspace(account.known_account)}
            </p>
          )}
        </section>
      )}
    </>
  );
}

function PendingAccount({
  account,
  busy,
  request,
}: {
  account: SubscriptionAccount;
  busy: boolean;
  request: RequestAction;
}) {
  return (
    <>
      {" "}
      {account.pending_account && (
        <section className="space-y-3 rounded-lg border border-amber-500 p-4" aria-label="Pending account replacement">
          <h3 className="font-semibold">A different account is waiting for confirmation</h3>
          <p className="text-sm">
            {account.token_valid
              ? "The current account remains active until you decide."
              : "Requests remain unavailable until you decide."}{" "}
            Confirm only if you intend to change accounts.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="min-w-0 break-words">
              <h4 className="font-medium">Current account</h4>
              <p>{account.known_account?.email || "—"}</p>
              <p className="text-sm">{workspace(account.known_account)}</p>
            </div>
            <div className="min-w-0 break-words">
              <h4 className="font-medium">New account</h4>
              <p>{account.pending_account.email || "—"}</p>
              <p className="text-sm">{workspace(account.pending_account)}</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={busy}
              onClick={() =>
                request(
                  ["pending/confirm"],
                  `Replace the account for ${subscriptionName(account)}?`,
                  `Use ${account.pending_account?.email || "the new account"} for future requests?`,
                )
              }
            >
              Use new account
            </Button>
            <Button variant="outline" disabled={busy} onClick={() => request(["pending/discard"])}>
              Discard new account
            </Button>
          </div>
        </section>
      )}
    </>
  );
}

function SubscriptionModels({ account }: { account: SubscriptionAccount }) {
  const canAddModel = account.token_valid && !account.retired && account.subscription_id;
  return (
    <section className="space-y-2">
      <h3 className="font-semibold">Subscription models</h3>
      {account.available_models ? (
        <>
          <ul className="flex flex-wrap gap-2">
            {account.available_models.models.map((model) => (
              <li
                key={model.slug}
                className="max-w-full break-words rounded-md border px-2 py-1 text-sm"
                title={model.name}
              >
                {model.slug}
                <span className="ml-2 text-muted-foreground">{model.configured ? "Configured" : "Not configured"}</span>
                {canAddModel && (
                  <Link
                    className="ml-2 text-primary underline"
                    href={subscriptionModelHref(account.subscription_id!, model.slug)}
                    aria-label={`Add model ${model.slug}`}
                  >
                    Add model
                  </Link>
                )}
              </li>
            ))}
          </ul>
          {account.available_models.models.length === 0 && (
            <p className="text-sm text-muted-foreground">No models returned by the provider.</p>
          )}
          {account.available_models.missing.length > 0 && (
            <p className="break-words text-sm text-destructive">
              Configured but missing from this subscription:{" "}
              {account.available_models.missing.map((model) => `${model.model_name} (${model.slug})`).join(", ")}
            </p>
          )}
        </>
      ) : (
        <p className="text-sm text-muted-foreground">
          {account.available_models_error
            ? "The model catalog is temporarily unavailable."
            : "Sign in to see the model catalog."}
        </p>
      )}
    </section>
  );
}

function problemDescription(problem: SubscriptionOverview["problems"][number]) {
  switch (problem.problem) {
    case "credential_missing":
      return `Credential ${problem.credential || ""} is missing.`;
    case "no_subscription":
      return "No subscription is assigned.";
    case "unknown_subscription":
      return `Subscription ${problem.account || ""} is not configured.`;
    default:
      return "Check its subscription assignment.";
  }
}

function AccountDetails({ account }: { account: SubscriptionAccount }) {
  const quota = account.quota?.limits.find((group) => group.id === "codex");
  return (
    <div className="grid gap-5 lg:grid-cols-3">
      <section className="min-w-0 space-y-2">
        <h3 className="font-semibold">Account</h3>
        <dl className="space-y-2 break-words text-sm">
          <div>
            <dt className="text-muted-foreground">Email</dt>
            <dd>{account.email || "—"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Workspace</dt>
            <dd>{workspace(account)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Credentials</dt>
            <dd>{account.credentials.join(", ") || "None assigned"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Session expires</dt>
            <dd>{date(account.token_expires_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Last renewed</dt>
            <dd>{date(account.last_refresh_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Automatic sign-in</dt>
            <dd>{account.auto_login ? "Enabled" : "Disabled"}</dd>
          </div>
        </dl>
      </section>
      <section className="space-y-3">
        <h3 className="font-semibold">Quota</h3>
        {quota?.primary && <WindowQuota window={quota.primary} />}
        {quota?.secondary && <WindowQuota window={quota.secondary} />}
        {!quota?.primary && !quota?.secondary && (
          <p className="text-sm text-muted-foreground">
            {account.quota_error ? "Quota is temporarily unavailable." : "No quota information."}
          </p>
        )}
      </section>
      <section className="min-w-0 space-y-2">
        <h3 className="font-semibold">Usage</h3>
        <p className="text-sm">Requests in 24 hours: {account.requests_24h ?? "Unavailable"}</p>
        <p className="break-words text-sm text-muted-foreground">
          {account.models.join(" · ") || "No models assigned"}
        </p>
      </section>
    </div>
  );
}

function AccountCard({
  account,
  busy,
  act,
  rename,
}: {
  account: SubscriptionAccount;
  busy: boolean;
  act: (request: ActionRequest) => void;
  rename: (account: SubscriptionAccount) => void;
}) {
  const quota = account.quota?.limits.find((group) => group.id === "codex");
  const request: RequestAction = (steps, title, description) => {
    const operation = { account, steps, title, description };
    act(operation);
  };
  let state = STATES[account.state] || "Status unavailable";
  if (quota?.allowed === false && account.token_valid) state = "Quota exhausted";
  if (account.pending_account) state = "Account confirmation required";
  if (account.retired) state = "Retired";

  return (
    <Card role="region" aria-label={`Subscription ${subscriptionName(account)}`}>
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <CardTitle className="break-words">{subscriptionName(account)}</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">{state}</p>
        </div>
        {!account.retired && (
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={busy} onClick={() => rename(account)}>
              Rename
            </Button>
            {(account.state === "ok" || account.state === "token_expired") && (
              <Button variant="outline" disabled={busy} onClick={() => request(["refresh"])}>
                Refresh session
              </Button>
            )}
            {account.state === "login_pending" ? (
              <>
                <Button variant="outline" disabled={busy} onClick={() => request(["login/cancel", "login"])}>
                  Request new code
                </Button>
                <Button variant="outline" disabled={busy} onClick={() => request(["login/cancel"])}>
                  Cancel sign-in
                </Button>
              </>
            ) : (
              <Button
                disabled={busy}
                onClick={() =>
                  request(
                    ["login"],
                    account.token_valid ? "Sign in again?" : undefined,
                    "The current account remains active while you complete the new sign-in.",
                  )
                }
              >
                {account.token_valid ? "Sign in again" : "Start sign-in"}
              </Button>
            )}
            {account.state !== "logged_out" && (
              <Button
                variant="destructive"
                disabled={busy}
                onClick={() =>
                  request(
                    ["logout"],
                    `Disconnect ${subscriptionName(account)}?`,
                    "New requests cannot use this subscription until you sign in again. Requests already sent may finish. This also clears the remembered account.",
                  )
                }
              >
                Disconnect
              </Button>
            )}
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                request(
                  ["retire"],
                  `Retire ${subscriptionName(account)}?`,
                  "This removes its saved connection and prevents future sign-in or model use. Requests already sent may finish. Its history remains available.",
                )
              }
            >
              Retire subscription
            </Button>
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-5">
        {!account.retired && (
          <>
            <DeviceSignIn account={account} busy={busy} />
            <PendingAccount account={account} busy={busy} request={request} />
          </>
        )}
        {account.error && (
          <p role="alert" className="text-sm text-destructive">
            {ERRORS[account.error.code] || "An account operation failed. Retry or sign in again."}
          </p>
        )}
        <AccountDetails account={account} />
        {!account.retired && <SubscriptionModels account={account} />}
      </CardContent>
    </Card>
  );
}

function SubscriptionNameDialog({
  nameDialog,
  displayName,
  setDisplayName,
  busy,
  failed,
  close,
  save,
}: {
  nameDialog: { account?: SubscriptionAccount } | null;
  displayName: string;
  setDisplayName: (name: string) => void;
  busy: boolean;
  failed: boolean;
  close: () => void;
  save: () => void;
}) {
  const saveLabel = nameDialog?.account ? "Save name" : "Add subscription";
  return (
    <Dialog
      open={nameDialog !== null}
      onOpenChange={(open) => {
        if (!open && !busy) close();
      }}
    >
      <DialogContent showCloseButton={!busy}>
        <DialogHeader>
          <DialogTitle>{nameDialog?.account ? "Rename subscription" : "Add subscription"}</DialogTitle>
          <DialogDescription>
            {nameDialog?.account
              ? "Changing the name preserves its connection, model assignments and history."
              : "Give this subscription a name. You can sign in after adding it."}
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (nameDialog && !busy && displayName.trim()) save();
          }}
        >
          <label className="block space-y-2" htmlFor="subscription-display-name">
            <span>Subscription name</span>
            <Input
              id="subscription-display-name"
              required
              maxLength={100}
              autoComplete="off"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              disabled={busy}
            />
          </label>
          {failed && <p role="alert">The subscription could not be saved. Check the name and try again.</p>}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={() => close()}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy || !displayName.trim()}>
              {busy ? "Saving…" : saveLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RetiredSubscriptions({
  accounts,
  act,
  rename,
}: {
  accounts: SubscriptionAccount[];
  act: (request: ActionRequest) => void;
  rename: (account: SubscriptionAccount) => void;
}) {
  return (
    <>
      {accounts.some((account) => account.retired) && (
        <details className="space-y-4">
          <summary className="cursor-pointer font-medium">
            Retired subscriptions ({accounts.filter((account) => account.retired).length})
          </summary>
          {accounts
            .filter((account) => account.retired)
            .map((account) => (
              <AccountCard key={account.account} account={account} busy={false} act={act} rename={rename} />
            ))}
        </details>
      )}
    </>
  );
}

function AffectedModels({ confirmation }: { confirmation: ActionRequest | null }) {
  return (
    <>
      {confirmation?.steps.some((step) => step === "logout" || step === "retire") && (
        <section aria-label="Affected models" className="space-y-2 text-sm">
          <h3 className="font-medium">Affected models</h3>
          {confirmation.account.models.length ? (
            <ul className="list-inside list-disc break-words">
              {confirmation.account.models.map((model) => (
                <li key={model}>{model}</li>
              ))}
            </ul>
          ) : (
            <p>No models assigned.</p>
          )}
        </section>
      )}
    </>
  );
}

function Subscriptions({ accessToken, userId }: { accessToken: string; userId: string | null }) {
  const queryClient = useQueryClient();
  const key = ["chatgpt-subscriptions", userId];
  const [confirmation, setConfirmation] = useState<ActionRequest | null>(null);
  const [nameDialog, setNameDialog] = useState<{ account?: SubscriptionAccount } | null>(null);
  const [displayName, setDisplayName] = useState("");
  const nameOptions: UseMutationOptions<
    SubscriptionAccount,
    Error,
    { account?: SubscriptionAccount; displayName: string }
  > = {
    mutationFn: ({ account, displayName }) =>
      account
        ? renameSubscription(accessToken, account.account, displayName)
        : createSubscription(accessToken, displayName),
    onMutate: () => queryClient.cancelQueries({ queryKey: key }),
    onSuccess: () => setNameDialog(null),
    onSettled: () => queryClient.invalidateQueries({ queryKey: key }),
  };
  const nameMutation = useMutation(nameOptions);
  const openNameDialog = (account?: SubscriptionAccount) => {
    nameMutation.reset();
    setDisplayName(account ? subscriptionName(account) : "");
    setNameDialog({ account });
  };
  const mutationOptions: UseMutationOptions<void, Error, ActionRequest> = {
    mutationFn: async ({ account, steps }: ActionRequest) => {
      for (const step of steps) await updateSubscription(accessToken, account.account, step);
    },
    onMutate: () => queryClient.cancelQueries({ queryKey: key }),
    onSuccess: () => setConfirmation(null),
    onSettled: () => queryClient.invalidateQueries({ queryKey: key }),
  };
  const mutation = useMutation(mutationOptions);
  const options: UseQueryOptions<SubscriptionOverview, Error> = {
    queryKey: key,
    queryFn: ({ signal }) => getSubscriptions(accessToken, signal),
    enabled: !mutation.isPending && !nameMutation.isPending,
    retry: false,
    refetchInterval: (state) => {
      if (state.state.error instanceof ApiError && [401, 403].includes(state.state.error.status)) return false;
      return state.state.data?.accounts.some((account) => account.state === "login_pending") ? 3000 : 15000;
    },
  };
  const query = useQuery(options);
  const authError = [query.error, mutation.error, nameMutation.error].find(
    (error) => error instanceof ApiError && [401, 403].includes(error.status),
  );
  const act = (request: ActionRequest) => {
    mutation.reset();
    if (request.title) setConfirmation(request);
    else mutation.mutate(request);
  };
  const busy = mutation.isPending || nameMutation.isPending;
  const unavailable = query.isPending || query.isError;
  if (authError)
    return (
      <div role="alert" className="p-6">
        <h1 className="text-xl font-semibold">Administrator access required</h1>
        <p className="my-3">Your session has expired or no longer has permission to manage subscriptions.</p>
        <a className="text-primary underline" href={uiHref("login")}>
          Sign in again
        </a>
      </div>
    );

  return (
    <div className="mx-auto max-w-7xl space-y-6 p-4 sm:p-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">ChatGPT subscriptions</h1>
          <p className="mt-1 text-sm text-muted-foreground">Manage the subscription accounts used by this gateway.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button disabled={unavailable || busy} onClick={() => openNameDialog()}>
            Add subscription
          </Button>
          <Button
            variant="outline"
            disabled={query.isFetching || mutation.isPending || nameMutation.isPending}
            onClick={() => {
              mutation.reset();
              void query.refetch();
            }}
          >
            Refresh status
          </Button>
        </div>
      </header>
      {query.isPending && <p role="status">Loading subscriptions…</p>}
      {query.isError && <p role="alert">Subscriptions could not be refreshed. Retry with Refresh status.</p>}
      {mutation.isError && <p role="alert">The action could not be completed. Refresh the status before retrying.</p>}
      {query.data && (
        <>
          <p className="text-sm text-muted-foreground">
            {query.data.accounts.filter((account) => !account.retired && account.token_valid).length} of{" "}
            {query.data.accounts.filter((account) => !account.retired).length} subscriptions signed in
          </p>
          {query.data.problems.length > 0 && (
            <section role="alert" className="space-y-2 rounded-lg border border-amber-500 p-4">
              <h2 className="font-semibold">Model configuration needs attention</h2>
              <ul className="list-inside list-disc break-words text-sm">
                {query.data.problems.map((problem, index) => (
                  <li key={`${problem.deployment}-${index}`}>
                    {problem.deployment}: {problemDescription(problem)}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {query.data.accounts.filter((account) => !account.retired).length === 0 && (
            <p>No active subscriptions. Choose Add subscription to get started.</p>
          )}
          {query.data.accounts
            .filter((account) => !account.retired)
            .map((account) => (
              <AccountCard
                key={account.account}
                account={account}
                busy={busy || query.isFetching || query.isError}
                act={act}
                rename={openNameDialog}
              />
            ))}
          <RetiredSubscriptions accounts={query.data.accounts} act={act} rename={openNameDialog} />
        </>
      )}
      <SubscriptionNameDialog
        nameDialog={nameDialog}
        displayName={displayName}
        setDisplayName={setDisplayName}
        busy={nameMutation.isPending}
        failed={nameMutation.isError}
        close={() => setNameDialog(null)}
        save={() => {
          if (nameDialog) nameMutation.mutate({ ...nameDialog, displayName: displayName.trim() });
        }}
      />
      <Dialog
        open={!!confirmation}
        onOpenChange={(open) => {
          if (!open && !mutation.isPending) setConfirmation(null);
        }}
      >
        <DialogContent showCloseButton={!mutation.isPending}>
          <DialogHeader>
            <DialogTitle>{confirmation?.title}</DialogTitle>
            <DialogDescription>{confirmation?.description}</DialogDescription>
          </DialogHeader>
          <AffectedModels confirmation={confirmation} />
          {mutation.isError && (
            <p role="alert">The action could not be completed. Close this dialog and refresh the status.</p>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={mutation.isPending} onClick={() => setConfirmation(null)}>
              Cancel
            </Button>
            <Button
              disabled={mutation.isPending}
              onClick={() => {
                if (confirmation) mutation.mutate(confirmation);
              }}
            >
              {mutation.isPending ? "Working…" : "Confirm"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function ChatGPTSubscriptionsPage() {
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
        Proxy administrator access is required to manage ChatGPT subscriptions.
      </p>
    );
  return <Subscriptions key={`${userId}:${accessToken}`} accessToken={accessToken} userId={userId} />;
}
