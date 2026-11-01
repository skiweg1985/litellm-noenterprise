import type { UseFormReturn } from "react-hook-form";
import type { ModelEditFormValues } from "../ModelInfoEditForm";
import type { CredentialItem } from "../networking";
import { FormField } from "../shared/form/FormField";
import { SearchSelect } from "../shared/SearchSelect";
import { credentialOptions } from "../shared/credentialOptions";
import { CredentialNameHint } from "../shared/CredentialIdentity";

export function ModelCredentialField({
  form,
  credentials,
}: {
  form: UseFormReturn<ModelEditFormValues>;
  credentials: CredentialItem[];
}) {
  return (
    <FormField control={form.control} name="litellm_credential_name">
      {({ id, value, onChange }) => (
        <>
          <SearchSelect
            inputId={id}
            placeholder="Select or search for existing credentials"
            options={credentialOptions(credentials)}
            value={(value as string | null) ?? ""}
            allowClear={false}
            onValueChange={(selected) => {
              if (selected !== null) onChange(selected === "" ? null : selected);
            }}
          />
          <CredentialNameHint credentialName={value as string | null | undefined} credentials={credentials} />
        </>
      )}
    </FormField>
  );
}
