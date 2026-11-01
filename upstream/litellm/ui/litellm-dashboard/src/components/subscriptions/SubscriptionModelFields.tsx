import { useFormContext, useWatch } from "react-hook-form";
import { MountedFormField, type MountedFormValues } from "@/components/common_components/MountedFormField";
import { MultiSelect } from "@/components/shared/MultiSelect";
import { subscriptionProblem, useModelSubscriptions } from "./SubscriptionPicker";
import { SubscriptionAssignmentEditor, type SubscriptionSelection } from "./SubscriptionAssignmentEditor";

export function SubscriptionModelFields({ accessToken }: { accessToken: string | null }) {
  const form = useFormContext<MountedFormValues>();
  const legacyId = useWatch({ control: form.control, name: "subscription_id" });
  const raw = useWatch({ control: form.control, name: "subscription_selection" }) as SubscriptionSelection | undefined;
  const selection = raw ?? { mode: "fixed", subscription_ids: typeof legacyId === "string" ? [legacyId] : [] };
  const chosenModels = useWatch({ control: form.control, name: "model" });
  const query = useModelSubscriptions(accessToken);
  const accounts = query.isError ? [] : query.data?.accounts ?? [];
  const selected = selection.subscription_ids.map((id) => accounts.find((item) => item.subscription_id === id));
  const compatibleModels = (selected[0]?.available_models?.models ?? []).filter((model) =>
    selected.every((account) => !subscriptionProblem(account, model.slug)),
  );
  const validateSelection = () => {
    if (query.isError) return "Refresh subscriptions and try again.";
    if (!selected.length) return "Select at least one subscription.";
    return selected.map((account) => subscriptionProblem(account)).find(Boolean) ?? true;
  };
  const validateModels = (value: unknown) => {
    if (!Array.isArray(value) || value.length === 0) return "Select a model available in every assigned subscription.";
    return (
      value.every((model) => selected.every((account) => !subscriptionProblem(account, String(model)))) ||
      "Select a model available in every assigned subscription."
    );
  };
  const selectSubscription = (next: SubscriptionSelection) => {
    form.setValue("subscription_selection", next, { shouldDirty: true, shouldValidate: true });
    form.setValue("subscription_id", next.subscription_ids[0] ?? null);
    const addedAccounts = next.subscription_ids
      .filter((id) => !selection.subscription_ids.includes(id))
      .map((id) => accounts.find((account) => account.subscription_id === id));
    // Routing changes do not invalidate a model. Unknown catalogs must not erase
    // the draft either; validation keeps submission blocked until they are known.
    if (
      Array.isArray(chosenModels) &&
      chosenModels.some((model) =>
        addedAccounts.some((account) => account?.available_models && subscriptionProblem(account, String(model))),
      )
    ) {
      form.setValue("model", []);
      form.setValue("model_mappings", []);
    }
  };
  return (
    <>
      <MountedFormField
        name="subscription_selection"
        label="ChatGPT subscriptions"
        required
        className="mb-4"
        defaultValue={selection}
        rules={{ validate: validateSelection }}
      >
        {() => (
          <SubscriptionAssignmentEditor
            accessToken={accessToken}
            value={selection}
            models={Array.isArray(chosenModels) ? (chosenModels as string[]) : []}
            onChange={selectSubscription}
          />
        )}
      </MountedFormField>
      <MountedFormField name="model" label="Subscription model(s)" required rules={{ validate: validateModels }}>
        {(control) => (
          <MultiSelect
            id={control.id}
            placeholder="Select subscription models"
            value={Array.isArray(control.value) ? control.value : []}
            options={compatibleModels.map((model) => ({
              value: `chatgpt/${model.slug}`,
              label: model.name || model.slug,
            }))}
            disabled={validateSelection() !== true}
            onValueChange={control.onChange}
          />
        )}
      </MountedFormField>
    </>
  );
}
