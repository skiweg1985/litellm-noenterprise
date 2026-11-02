"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SearchSelect } from "@/components/shared/SearchSelect";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogFooter,
} from "@/components/ui/alert-dialog";
import { ApiError } from "@/lib/http/client";
import { getOIDCLinks, updateOIDCLink, type LinkAction, type LinkView, type UserChoice } from "@/lib/http/oidcLinks";
import { formatUserRole } from "@/utils/roles";

const stateLabels = {
  pending: "Review pending",
  linked: "Linked to OIDC",
  rejected: "Request rejected",
  unlinked: "Unlinked",
};
const actionLabels: Record<LinkAction, string> = {
  approve: "Confirm link",
  reject: "Reject request",
  reopen: "Reopen review",
  unlink: "Unlink OIDC",
};
const statusLabels = {
  none: "Not linked to SSO",
  unconfirmed_oidc: "Existing OIDC association requires administrator confirmation",
  independent_oidc: "Linked to independent OIDC",
  legacy: "Existing legacy SSO link",
};

type Decision = { action: LinkAction; link: LinkView; target?: UserChoice };

const successMessages: Record<LinkAction, string> = {
  approve:
    "Account linked. Ask the person to sign in again; that sign-in checks access and applies the current OIDC role rules.",
  unlink:
    "OIDC link removed. OIDC sessions are revoked. The account, native keys and history are retained; linking again requires review.",
  reject: "Request rejected. The account is unchanged.",
  reopen: "Review reopened. Confirm the identity before approving a link.",
};

function DecisionDescription({ decision }: { decision: Decision | null }) {
  if (!decision) return null;
  switch (decision.action) {
    case "approve":
      return (
        <>
          Link this verified identity to{" "}
          <strong className="break-all">{decision.target?.user_email ?? decision.target?.user_id}</strong>? Current
          role: {formatUserRole(decision.target?.user_role ?? "")}. OIDC role preview:{" "}
          {formatUserRole(decision.link.proposed_role)}. The next fresh sign-in checks access and applies the current
          role rules. Existing account data stays in place.
        </>
      );
    case "unlink":
      return (
        <>
          Revoke this identity&apos;s OIDC sessions and remove its account link? Native keys, teams, budgets, history
          and personal connection records stay intact. This is not a full account suspension. A new link requires
          administrator review.
        </>
      );
    case "reject":
      return (
        <>
          Reject this request? No account is changed and this identity cannot register automatically. You can reopen the
          review later.
        </>
      );
    case "reopen":
      return (
        <>
          Return this identity to review? This does not grant access or create a session. Changed provider settings
          require the person to sign in again before approval.
        </>
      );
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409)
      return "This link or user changed elsewhere. Reload links and review the current state before trying again.";
    if (error.status === 401 || error.status === 403)
      return "A full administrator sign-in is required to manage OIDC account links.";
    if (error.status === 422) return "The request could not be validated. Reload links and select an available user.";
    if (error.body && typeof error.body === "object" && "detail" in error.body) {
      const detail = error.body.detail;
      if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string")
        return detail.message;
    }
  }
  return "The server response could not be confirmed. Reload links to check whether the action completed before trying again.";
}

function LinkCard({
  link,
  users,
  disabled,
  decide,
}: {
  link: LinkView;
  users: UserChoice[];
  disabled: boolean;
  decide: (decision: Decision) => void;
}) {
  const [targetId, setTargetId] = useState<string | null>(null);
  const available = (user: UserChoice) =>
    user.sso_status === "none" || (user.sso_status === "unconfirmed_oidc" && user.user_id === link.identity_key);
  const target = users.find((user) => user.user_id === targetId && available(user));
  const linkedUser = users.find((user) => user.user_id === link.user_id);
  const choices = users.filter(available);
  const pending = link.state === "pending";
  return (
    <Card role="region" className="min-w-0" aria-label={`Identity ${link.subject}`}>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center justify-between gap-2 text-base">
          <span className="break-all">{link.email ?? link.subject}</span>
          <span className="rounded bg-muted px-2 py-1 text-xs font-medium">{stateLabels[link.state]}</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <dl className="grid gap-2 sm:grid-cols-[9rem_minmax(0,1fr)]">
          <dt className="text-muted-foreground">Verified provider</dt>
          <dd className="break-all">{link.issuer}</dd>
          <dt className="text-muted-foreground">Stable identity</dt>
          <dd className="break-all">{link.subject}</dd>
          {link.user_id && (
            <>
              <dt className="text-muted-foreground">LiteLLM user</dt>
              <dd className="break-all">
                {linkedUser?.user_email ? `${linkedUser.user_email} · ` : ""}
                {link.user_id}
              </dd>
            </>
          )}
        </dl>
        {!link.current_configuration && (
          <p role="status" className="rounded bg-muted p-3">
            OIDC settings changed. Ask this person to sign in again to refresh their verified identity before approval.
          </p>
        )}
        {pending && (
          <>
            <p>Email matches are suggestions only. Confirm the person&apos;s identity before linking an account.</p>
            {!!link.candidates.length && (
              <div>
                <p className="font-medium">Suggested accounts</p>
                <ul className="list-disc space-y-1 pl-5">
                  {link.candidates.map((id) => {
                    const user = users.find((candidate) => candidate.user_id === id);
                    return (
                      <li key={id} className="break-all">
                        {user?.user_email ?? id} · {id}
                        {user && !available(user)
                          ? ` — ${statusLabels[user.sso_status]}; cannot replace this link`
                          : ""}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
            <div className="space-y-2">
              <label htmlFor={`target-${link.identity_key}`} className="font-medium">
                Existing user account
              </label>
              <SearchSelect
                inputId={`target-${link.identity_key}`}
                aria-label={`Existing user account for ${link.subject}`}
                options={choices.map((user) => ({
                  value: user.user_id,
                  label: user.user_email ?? user.user_id,
                  sublabel: user.user_id,
                }))}
                value={targetId}
                onValueChange={setTargetId}
                placeholder="Search by email or user ID"
                emptyText="No unlinked user accounts available"
                disabled={disabled || !link.current_configuration}
              />
              {!choices.length && (
                <p>No unlinked accounts are available. Existing SSO links cannot be replaced here.</p>
              )}
            </div>
            {target && (
              <div className="rounded border p-3">
                {target.sso_status === "unconfirmed_oidc" && (
                  <p className="mb-2 font-medium">
                    {statusLabels.unconfirmed_oidc}. This confirms the existing association without replacing it.
                  </p>
                )}
                <p>
                  <strong>Current role:</strong> {formatUserRole(target.user_role ?? "")}
                </p>
                <p>
                  <strong>OIDC role preview:</strong> {formatUserRole(link.proposed_role)}
                </p>
                <p className="mt-2">
                  The next successful sign-in checks access again and applies the current OIDC role rules. Linking does
                  not sign this person in. Their user ID, teams, keys, budgets and history stay with this account.
                </p>
              </div>
            )}
          </>
        )}
        <div className="flex flex-wrap gap-2">
          {pending && (
            <>
              <Button
                disabled={disabled || !target || !link.current_configuration}
                onClick={() => decide({ action: "approve", link, target })}
              >
                Review link
              </Button>
              <Button variant="outline" disabled={disabled} onClick={() => decide({ action: "reject", link })}>
                Reject request
              </Button>
            </>
          )}
          {link.state === "linked" && (
            <Button variant="outline" disabled={disabled} onClick={() => decide({ action: "unlink", link })}>
              Unlink OIDC
            </Button>
          )}
          {(link.state === "rejected" || link.state === "unlinked") && (
            <Button variant="outline" disabled={disabled} onClick={() => decide({ action: "reopen", link })}>
              Reopen review
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function OIDCAccountLinks({ accessToken }: { accessToken: string }) {
  const queryClient = useQueryClient();
  const queryKey = ["oidc-account-links", accessToken];
  const query = useQuery({ queryKey, queryFn: ({ signal }) => getOIDCLinks(accessToken, signal), retry: false });
  const [filter, setFilter] = useState("");
  const [decision, setDecision] = useState<Decision | null>(null);
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const data = query.data;
  const needle = filter.trim().toLowerCase();
  const matches = (...values: (string | null | undefined)[]) =>
    values.some((value) => value?.toLowerCase().includes(needle));
  const links = data?.links.filter((link) => matches(link.email, link.subject, link.user_id)) ?? [];
  const users = data?.users.filter((user) => matches(user.user_id, user.user_email)) ?? [];

  async function reload() {
    setDecision(null);
    const result = await query.refetch();
    if (result.isSuccess) setActionError(null);
  }
  async function confirm() {
    if (!decision) return;
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      await updateOIDCLink(accessToken, decision.link.identity_key, decision.action, {
        expected_revision: decision.link.revision,
        ...(decision.action === "approve" ? { user_id: decision.target!.user_id } : {}),
      });
      setNotice(successMessages[decision.action]);
      setDecision(null);
      await queryClient.invalidateQueries({ queryKey });
      await queryClient.invalidateQueries({ queryKey: ["users"] });
    } catch (error) {
      setDecision(null);
      setActionError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }
  return (
    <section aria-labelledby="oidc-links-title" className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="oidc-links-title" className="text-lg font-semibold">
            OIDC account links
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Review sign-in identities and attach them to existing LiteLLM users.
          </p>
        </div>
        <Button variant="outline" onClick={reload} disabled={saving || query.isFetching}>
          Reload links
        </Button>
      </div>
      {query.isPending && <p role="status">Loading account links…</p>}
      {(query.error || actionError) && (
        <p role="alert" className="rounded border border-destructive p-3 text-sm">
          {actionError ?? errorMessage(query.error)}
        </p>
      )}
      {notice && (
        <p role="status" className="rounded border p-3 text-sm">
          {notice}
        </p>
      )}
      {data && (
        <>
          {!data.enabled && (
            <p role="status" className="rounded bg-muted p-3 text-sm">
              Independent OIDC is disabled. Enable it in Admin Settings → SSO Settings before users sign in.
            </p>
          )}
          <div className="max-w-lg space-y-1">
            <label htmlFor="oidc-link-filter" className="text-sm font-medium">
              Find identity or user
            </label>
            <Input
              id="oidc-link-filter"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="Email, stable identity or user ID"
            />
          </div>
          <div className="grid min-w-0 gap-4">
            {links.map((link) => (
              <LinkCard
                key={`${link.identity_key}:${link.revision}`}
                link={link}
                users={data.users}
                disabled={saving || !!actionError || query.isFetching}
                decide={setDecision}
              />
            ))}
            {!links.length && (
              <p className="text-sm text-muted-foreground">No identity requests or links match this view.</p>
            )}
          </div>
          <details className="rounded border p-4">
            <summary className="cursor-pointer font-medium">User sign-in status ({users.length})</summary>
            <ul className="mt-3 space-y-3 text-sm">
              {users.map((user) => (
                <li key={user.user_id} className="flex flex-wrap justify-between gap-2 border-b pb-2">
                  <span className="min-w-0 break-all">
                    {user.user_email ?? user.user_id}
                    <span className="block text-xs text-muted-foreground">{user.user_id}</span>
                  </span>
                  <span>{statusLabels[user.sso_status]}</span>
                </li>
              ))}
              {!users.length && <li>No matching users.</li>}
            </ul>
          </details>
        </>
      )}
      <AlertDialog
        open={!!decision}
        onOpenChange={(open) => {
          if (!open && !saving) setDecision(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogTitle>{decision ? actionLabels[decision.action] : "Review OIDC link"}</AlertDialogTitle>
          <AlertDialogDescription>
            <DecisionDescription decision={decision} />
          </AlertDialogDescription>
          <AlertDialogFooter>
            <Button variant="outline" disabled={saving} onClick={() => setDecision(null)}>
              Cancel
            </Button>
            <Button disabled={saving} onClick={confirm}>
              {saving ? "Saving…" : (decision && actionLabels[decision.action]) || "Confirm"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
