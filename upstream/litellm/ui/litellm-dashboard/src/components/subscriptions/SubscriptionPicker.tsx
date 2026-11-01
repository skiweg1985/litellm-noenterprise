"use client";

import { useQuery, type QueryFunctionContext } from "@tanstack/react-query";
import { getSubscriptions, type SubscriptionAccount } from "@/lib/http/chatgptSubscriptions";
import { SearchSelect } from "@/components/shared/SearchSelect";
import { Button } from "@/components/ui/button";

export function useModelSubscriptions(accessToken: string | null) {
  const options = {
    queryKey: ["chatgpt-subscriptions", "model-picker", accessToken],
    queryFn: ({ signal }: QueryFunctionContext) => getSubscriptions(accessToken!, signal),
    enabled: Boolean(accessToken),
    retry: false,
    staleTime: 15_000,
  };
  return useQuery(options);
}

export function subscriptionProblem(account: SubscriptionAccount | undefined, model?: string): string | null {
  if (!account) return "Select a subscription.";
  if (account.state === "credential_rejected") return "Sign-in was rejected. Reconnect this subscription.";
  if (!account.token_valid && account.state === "token_expired") return "Token renewal is required. Refresh subscriptions to check this connection.";
  if (account.retired || !account.token_valid)
    return "This subscription is disconnected or retired. Sign in or select another subscription.";
  if (!account.available_models)
    return "The subscription model catalog is unavailable. Refresh subscriptions and try again.";
  const slug = model?.replace(/^chatgpt\//, "");
  if (slug && !account.available_models.models.some((entry) => entry.slug === slug)) {
    return "This model is not available in the selected subscription.";
  }
  return null;
}

export function SubscriptionPicker({
  accessToken,
  value,
  onChange,
  model,
  inputId,
  disabled = false,
}: {
  accessToken: string | null;
  value: string | null | undefined;
  onChange: (value: string | null) => void;
  model?: string;
  inputId?: string;
  disabled?: boolean;
}) {
  const query = useModelSubscriptions(accessToken);
  const accounts = query.isError ? [] : query.data?.accounts ?? [];
  const selected = accounts.find((account) => account.subscription_id === value);
  const options = accounts
    .filter((account) => account.subscription_id === value || !subscriptionProblem(account, model))
    .map((account) => ({ value: account.subscription_id!, label: account.display_name || account.account }));
  const problem = value && !query.isPending ? subscriptionProblem(selected, model) : null;
  return (
    <div className="space-y-2">
      <SearchSelect
        key={query.isError ? "unavailable" : "available"}
        inputId={inputId}
        aria-label="ChatGPT subscription"
        value={value}
        options={options}
        onValueChange={onChange}
        disabled={disabled || query.isPending || query.isError}
        allowClear={false}
        placeholder={query.isPending ? "Loading subscriptions…" : "Select a subscription"}
        emptyText="No connected subscription offers this model"
      />
      {query.isError && <p role="alert">Subscriptions could not be loaded. Refresh and try again.</p>}
      {!query.isError && problem && (
        <p role="status" className="text-sm text-muted-foreground">
          {problem}
        </p>
      )}
      {!disabled && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          Refresh subscriptions
        </Button>
      )}
    </div>
  );
}
