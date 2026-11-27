import { useState } from "react";
import { useIsMutating, useQuery } from "@tanstack/react-query";
import { MemberPicker, groupLabel } from "./MemberPicker";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  type Access,
  type AdminAccount,
  type FairUse,
  getAccess,
  getFairUse,
  saveAccess,
  saveFairUse,
} from "@/lib/http/subscriptionFairUse";
import { fairUseKey, type Session, useSaveSetting, useDirectoryNames } from "./queries";
import { shortWindowNames } from "./presentation";

type PolicyProps = { session: Session; account: AdminAccount };
const groups = ["teams", "users"] as const;
function Members({
  access,
  unknown,
  session,
  onChange,
}: {
  access: Access;
  unknown: Access;
  session: Session;
  onChange?: (next: Access) => void;
}) {
  const names = useDirectoryNames(session, access.users ?? []);
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {groups.map((group) => {
        const ids = access[group] ?? [];
        return (
          <div key={group} className="min-w-0 space-y-2">
            {onChange && (
              <MemberPicker
                group={group}
                onSelect={(id) => {
                  if (id && !ids.includes(id)) onChange({ ...access, [group]: [...ids, id] });
                }}
              />
            )}
            {!onChange && ids.length > 0 && <p className="font-medium">{groupLabel[group]}s</p>}
            <ul className="space-y-1">
              {ids.map((id) => {
                const name = unknown[group]?.includes(id)
                  ? "Unknown " + groupLabel[group].toLowerCase() + " · " + id
                  : names[group].get(id) ?? id;
                return (
                  <li key={id} className="flex min-w-0 items-center justify-between gap-2">
                    <span className="break-all">{name}</span>
                    {onChange && (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={"Remove " + name}
                        onClick={() => onChange({ ...access, [group]: ids.filter((value) => value !== id) })}
                      >
                        Remove
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

function AccessEditor({ account, session, initial, close }: PolicyProps & { initial: Access; close: () => void }) {
  const [access, setAccess] = useState(initial);
  const save = useSaveSetting(
    session,
    [account.account, "access"],
    (body: Access) => saveAccess(session.accessToken, account.account, body),
    close,
  );
  return (
    <fieldset disabled={save.isPending} className="min-w-0 space-y-3">
      <Members access={access} unknown={account.unknown_ids} session={session} onChange={setAccess} />
      {!access.teams?.length && !access.users?.length && <p>Everyone</p>}
      {save.isError && <p role="alert">Could not save access. Try again.</p>}
      <Button onClick={() => save.mutate(access)}>Save access</Button>
    </fieldset>
  );
}

function FairUseEditor({ account, session, initial, close }: PolicyProps & { initial: FairUse; close: () => void }) {
  const [enabled, setEnabled] = useState(initial.enabled);
  const [primary, setPrimary] = useState(String(initial.primary_threshold));
  const [secondary, setSecondary] = useState(String(initial.secondary_threshold));
  const save = useSaveSetting(
    session,
    [account.account, "fair-use"],
    (body: FairUse) => saveFairUse(session.accessToken, account.account, body),
    close,
  );
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate({ enabled, primary_threshold: Number(primary), secondary_threshold: Number(secondary) });
      }}
    >
      <fieldset className="space-y-4" disabled={save.isPending}>
        <label className="flex items-center gap-2">
          <Switch checked={enabled} onCheckedChange={setEnabled} disabled={save.isPending} />
          Enable fair use
        </label>
        <fieldset className="grid gap-3 sm:grid-cols-2">
          <legend className="mb-1 font-medium">Start limiting at</legend>
          <label>
            5-hour window (%)
            <Input
              type="number"
              min={1}
              max={100}
              required
              value={primary}
              onChange={(event) => setPrimary(event.target.value)}
            />
          </label>
          <label>
            Weekly window (%)
            <Input
              type="number"
              min={1}
              max={100}
              required
              value={secondary}
              onChange={(event) => setSecondary(event.target.value)}
            />
          </label>
        </fieldset>
        {save.isError && <p role="alert">Could not save fair use. Try again.</p>}
        <Button type="submit">Save fair use</Button>
      </fieldset>
    </form>
  );
}

function PolicyDialog({
  session,
  account,
  kind,
  close,
}: PolicyProps & { kind: "access" | "fair-use"; close: () => void }) {
  const prefix = [...fairUseKey(session), account.account];
  const saving = useIsMutating({ mutationKey: [...prefix, kind] }) > 0;
  const readOptions = (setting: typeof kind) => ({
    queryKey: [...prefix, setting],
    enabled: kind === setting,
    retry: false,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const access = useQuery({
    ...readOptions("access"),
    queryFn: ({ signal }) => getAccess(session.accessToken, account.account, signal),
  });
  const policy = useQuery({
    ...readOptions("fair-use"),
    queryFn: ({ signal }) => getFairUse(session.accessToken, account.account, signal),
  });
  const query = kind === "access" ? access : policy;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !saving) close();
      }}
    >
      <DialogContent showCloseButton={!saving} className="max-h-[85dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{kind === "access" ? "Edit access" : "Edit fair use"}</DialogTitle>
        </DialogHeader>
        {query.isError && (
          <p role="alert">
            Could not load settings.{" "}
            <Button variant="outline" onClick={() => void query.refetch()}>
              Retry
            </Button>
          </p>
        )}
        {query.isPending && <p role="status">Loading settings…</p>}
        {query.isSuccess && (
          <>
            {kind === "access" && access.data && (
              <AccessEditor session={session} account={account} initial={access.data} close={close} />
            )}
            {kind === "fair-use" && policy.data && (
              <FairUseEditor session={session} account={account} initial={policy.data} close={close} />
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function PolicyCard({ account, session, retired }: PolicyProps & { retired: boolean }) {
  const [editing, setEditing] = useState<"access" | "fair-use" | null>(null);
  const [expanded, setExpanded] = useState(false);
  const teams = account.access.teams?.length ?? 0;
  const users = account.access.users?.length ?? 0;
  return (
    <section aria-label="Subscription settings" className="grid gap-3 rounded-lg border p-3 text-sm md:grid-cols-2">
      <div className="min-w-0 space-y-1">
        <h3 className="font-semibold">Access</h3>
        {teams + users ? (
          <details onToggle={(event) => setExpanded(event.currentTarget.open)}>
            <summary className="cursor-pointer">
              {[
                teams && teams + (teams === 1 ? " team" : " teams"),
                users && users + (users === 1 ? " user" : " users"),
              ]
                .filter(Boolean)
                .join(" · ")}
            </summary>
            {expanded && <Members access={account.access} unknown={account.unknown_ids} session={session} />}
          </details>
        ) : (
          <p>Everyone</p>
        )}
        {!retired && (
          <Button size="sm" variant="outline" onClick={() => setEditing("access")}>
            Edit access
          </Button>
        )}
      </div>
      <div className="space-y-1">
        <h3 className="font-semibold">Fair use</h3>
        <p>
          {account.enabled ? "On" : "Off"}
          {" · limits from "}
          {account.windows.map((window) => window.threshold + "% (" + shortWindowNames[window.id] + ")").join(", ")}
        </p>
        {!retired && (
          <Button size="sm" variant="outline" onClick={() => setEditing("fair-use")}>
            Edit fair use
          </Button>
        )}
      </div>
      {editing && !retired && (
        <PolicyDialog session={session} account={account} kind={editing} close={() => setEditing(null)} />
      )}
    </section>
  );
}
