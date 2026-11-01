import { useEffect } from "react";
import { useWatch, type UseFormReturn } from "react-hook-form";
import { FormField } from "@/components/shared/form/FormField";
import type { ModelEditFormValues } from "@/components/ModelInfoEditForm";
import { useModelSubscriptions } from "./SubscriptionPicker";
import { SubscriptionAssignmentEditor } from "./SubscriptionAssignmentEditor";

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
  const legacyId = useWatch({ control: form.control, name: "subscription_id" });
  const selection = useWatch({ control: form.control, name: "subscription_selection" });
  const model = useWatch({ control: form.control, name: "litellm_model_name" });
  const accounts = query.isError ? [] : query.data?.accounts ?? [];
  const legacy = accounts.find((account) => account.deployment_ids?.includes(deploymentId));
  useEffect(() => {
    if (!form.getValues("subscription_selection") && legacy) {
      form.setValue("subscription_id", legacy.subscription_id);
      form.setValue("subscription_selection", { mode: "fixed", subscription_ids: [legacy.subscription_id] });
    }
  }, [form, legacy]);
  return (
    <section className="space-y-2" aria-label="Model subscription">
      <p className="text-sm font-medium">ChatGPT subscriptions</p>
      <FormField control={form.control} name="subscription_selection">
        {({ onChange }) => (
          <SubscriptionAssignmentEditor
            accessToken={accessToken}
            value={selection ?? { mode: "fixed", subscription_ids: legacyId ? [legacyId] : [] }}
            models={model ? [model] : []}
            disabled={!editable}
            onChange={(next) => {
              onChange(next);
              form.setValue("subscription_id", next.subscription_ids[0] ?? null);
            }}
          />
        )}
      </FormField>
      {!isDbModel && (
        <p className="text-sm text-muted-foreground">
          Configuration-backed model. Its subscription assignment is read-only here.
        </p>
      )}
    </section>
  );
}
