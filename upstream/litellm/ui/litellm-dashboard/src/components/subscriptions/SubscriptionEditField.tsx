import { useEffect } from "react";
import { useWatch, type UseFormReturn } from "react-hook-form";
import { FormField } from "@/components/shared/form/FormField";
import type { ModelEditFormValues } from "@/components/ModelInfoEditForm";
import { SubscriptionPicker, useModelSubscriptions } from "./SubscriptionPicker";

export function SubscriptionEditField({
  form,
  accessToken,
  deploymentId,
  editable,
  isDbModel,
}: {
  form: UseFormReturn<ModelEditFormValues>;
  accessToken: string | null;
  deploymentId: string;
  editable: boolean;
  isDbModel: boolean;
}) {
  const query = useModelSubscriptions(accessToken);
  const selectedId = useWatch({ control: form.control, name: "subscription_id" });
  const model = useWatch({ control: form.control, name: "litellm_model_name" });
  const accounts = query.isError ? [] : query.data?.accounts ?? [];
  const legacy = accounts.find((account) => account.deployment_ids?.includes(deploymentId));
  const selected = accounts.find((account) => account.subscription_id === selectedId) ?? legacy;
  useEffect(() => {
    if (!form.getValues("subscription_id") && legacy) form.setValue("subscription_id", legacy.subscription_id);
  }, [form, legacy]);
  return (
    <section className="space-y-2" aria-label="Model subscription">
      <p className="text-sm font-medium">ChatGPT subscription</p>
      {editable ? (
        <FormField control={form.control} name="subscription_id">
          {({ id, value, onChange }) => (
            <SubscriptionPicker
              inputId={id}
              accessToken={accessToken}
              value={value as string | null}
              onChange={onChange}
              model={model}
            />
          )}
        </FormField>
      ) : (
        <p>
          {selected?.display_name ||
            selected?.account ||
            (query.isPending ? "Loading subscription…" : "Subscription assignment unavailable")}
        </p>
      )}
      <p className="text-sm text-muted-foreground">Fixed subscription: requests use only this account.</p>
      {!isDbModel && (
        <p className="text-sm text-muted-foreground">
          Configuration-backed model. Its subscription assignment is read-only here.
        </p>
      )}
    </section>
  );
}
