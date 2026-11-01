import { useFormContext, useWatch } from "react-hook-form";
import { MountedFormField, type MountedFormValues } from "@/components/common_components/MountedFormField";
import { MultiSelect } from "@/components/shared/MultiSelect";
import { SubscriptionPicker, subscriptionProblem, useModelSubscriptions } from "./SubscriptionPicker";

export function SubscriptionModelFields({ accessToken }: { accessToken: string | null }) {
  const form = useFormContext<MountedFormValues>();
  const selectedId = useWatch({ control: form.control, name: "subscription_id" });
  const query = useModelSubscriptions(accessToken);
  const account = query.isError ? undefined : query.data?.accounts.find((item) => item.subscription_id === selectedId);
  const models = account?.available_models?.models ?? [];
  const selectSubscription = (id: string | null) => {
    form.setValue("subscription_id", id, { shouldDirty: true, shouldValidate: true });
    form.setValue("model", []);
    form.setValue("model_mappings", []);
  };
  const validateModels = (value: unknown) => {
    if (!Array.isArray(value) || value.length === 0) return "Select a model available in this subscription.";
    return (
      value.every((model) => !subscriptionProblem(account, String(model))) ||
      "Select a model available in this subscription."
    );
  };
  return (
    <>
      <MountedFormField
        name="subscription_id"
        label="ChatGPT subscription"
        required
        className="mb-4"
        rules={{
          validate: () =>
            query.isError ? "Refresh subscriptions and try again." : subscriptionProblem(account) ?? true,
        }}
      >
        {(control) => (
          <SubscriptionPicker
            inputId={control.id}
            accessToken={accessToken}
            value={control.value as string | null}
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
            options={models.map((model) => ({ value: `chatgpt/${model.slug}`, label: model.name || model.slug }))}
            disabled={Boolean(subscriptionProblem(account))}
            onValueChange={control.onChange}
          />
        )}
      </MountedFormField>
      <p className="mt-1 mb-4 text-sm text-muted-foreground">Fixed subscription: requests use only this account.</p>
    </>
  );
}
