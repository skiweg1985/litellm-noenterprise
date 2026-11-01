"use client";

import { SearchSelect } from "@/components/shared/SearchSelect";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SubscriptionPicker, subscriptionProblem, useModelSubscriptions } from "./SubscriptionPicker";

export type SubscriptionMode = "fixed" | "priority" | "distribution";
export interface SubscriptionSelection { mode: SubscriptionMode; subscription_ids: string[] }
export const SUBSCRIPTION_MODE_LABELS = {
  fixed: "Fixed subscription",
  priority: "Preferred subscription with reserves",
  distribution: "Distribute requests",
};
export const SUBSCRIPTION_MODE_DESCRIPTIONS = {
  fixed: "Fixed subscription: requests use only this account.",
  priority: "Use the first available subscription in this order. Temporary limits or provider outages can use an assigned reserve; no unassigned account is used.",
  distribution: "Distribute requests across the selected available subscriptions. List order does not set a preference.",
};

export function SubscriptionAssignmentEditor({ accessToken, value, onChange, models = [], disabled = false, enableDistribution = false }: {
  accessToken: string | null;
  value: SubscriptionSelection;
  onChange: (value: SubscriptionSelection) => void;
  models?: string[];
  disabled?: boolean;
  enableDistribution?: boolean;
}) {
  const query = useModelSubscriptions(accessToken);
  const accounts = query.isError ? [] : query.data?.accounts ?? [];
  const selected = value.subscription_ids.map((id) => accounts.find((account) => account.subscription_id === id));
  const candidates = accounts.filter((account) => !value.subscription_ids.includes(account.subscription_id)
    && !subscriptionProblem(account) && models.every((model) => !subscriptionProblem(account, model)));
  const updateIds = (subscription_ids: string[]) => onChange({ ...value, subscription_ids });
  const move = (index: number, offset: number) => {
    const ids = [...value.subscription_ids];
    [ids[index], ids[index + offset]] = [ids[index + offset], ids[index]];
    updateIds(ids);
  };
  const changeMode = (mode: SubscriptionMode) => onChange({ mode, subscription_ids: mode === "fixed" ? value.subscription_ids.slice(0, 1) : value.subscription_ids });
  const noneAvailable = selected.length > 0 && selected.every((account) => !account || account.retired || (!account.token_valid && account.state !== "token_expired"));
  return <div className="space-y-3">
    <Select value={value.mode} onValueChange={changeMode} disabled={disabled}>
      <SelectTrigger aria-label="Subscription routing"><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value="fixed">{SUBSCRIPTION_MODE_LABELS.fixed}</SelectItem>
        <SelectItem value="priority">{SUBSCRIPTION_MODE_LABELS.priority}</SelectItem>
        {(enableDistribution || value.mode === "distribution") && <SelectItem value="distribution">{SUBSCRIPTION_MODE_LABELS.distribution}</SelectItem>}
      </SelectContent>
    </Select>
    <p className="text-sm text-muted-foreground">{SUBSCRIPTION_MODE_DESCRIPTIONS[value.mode]}</p>
    {value.mode === "fixed" ? <SubscriptionPicker accessToken={accessToken} value={value.subscription_ids[0]} onChange={(id) => updateIds(id ? [id] : [])} model={models[0]} disabled={disabled} /> : <>
      {!query.isError && <ol aria-label="Assigned subscriptions" className="space-y-2">
        {value.subscription_ids.map((id, index) => {
          const account = selected[index];
          const name = account?.display_name || account?.account || "Unavailable subscription";
          const problem = subscriptionProblem(account) || models.map((model) => subscriptionProblem(account, model)).find(Boolean);
          return <li key={id} className="rounded-md border p-3 text-sm">
            <p className="font-medium break-words">{value.mode === "priority" ? `${index + 1}. ` : ""}{name}</p>
            <p className="text-muted-foreground">{problem || "Ready"}</p>
            {!disabled && <div className="mt-2 flex flex-wrap gap-2">
              {value.mode === "priority" && <>
                <Button type="button" variant="outline" size="sm" aria-label={`Move ${name} up`} disabled={index === 0} onClick={() => move(index, -1)}>Move up</Button>
                <Button type="button" variant="outline" size="sm" aria-label={`Move ${name} down`} disabled={index === selected.length - 1} onClick={() => move(index, 1)}>Move down</Button>
              </>}
              <Button type="button" variant="outline" size="sm" aria-label={`Remove ${name}`} onClick={() => updateIds(value.subscription_ids.filter((member) => member !== id))}>Remove</Button>
            </div>}
          </li>;
        })}
      </ol>}
      {!disabled && <SearchSelect aria-label="Add subscription" value={null} allowClear={false}
        options={candidates.map((account) => ({ value: account.subscription_id, label: account.display_name || account.account }))}
        onValueChange={(id) => { if (id) updateIds([...value.subscription_ids, id]); }}
        disabled={query.isPending || query.isError || selected.length >= 16}
        placeholder="Add a compatible subscription" emptyText="No other connected subscription offers every selected model" />}
      {selected.length >= 16 && <p className="text-sm text-muted-foreground">Up to 16 subscriptions can be assigned.</p>}
      {query.isError && <p role="alert">Subscriptions could not be loaded. Refresh and try again.</p>}
      {!disabled && <Button type="button" variant="ghost" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh subscriptions</Button>}
    </>}
    {!query.isPending && !query.isError && noneAvailable && <p role="status" className="text-sm">No assigned subscription is available. Sign in through <a className="underline" href="/ui/chatgpt-subscriptions/">subscription management</a> or remove and replace unavailable subscriptions.</p>}
  </div>;
}
