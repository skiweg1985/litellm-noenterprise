import { useId, type ReactNode } from "react";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ClaimRule } from "@/lib/http/oidcSettings";

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h3 className="text-base font-semibold">{title}</h3>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">{children}</CardContent>
    </Card>
  );
}
export function Field({
  label,
  value,
  onChange,
  hint,
  type = "text",
  required = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
  type?: string;
  required?: boolean;
}) {
  const id = useId();
  return (
    <div className="min-w-0 space-y-1">
      <label className="block text-sm font-medium" htmlFor={id}>
        {label}
      </label>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        type={type}
        required={required}
        autoComplete={type === "password" ? "new-password" : "off"}
        aria-describedby={hint ? `${id}-hint` : undefined}
      />
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
}
export function ListField({
  label,
  values,
  onChange,
  hint = "One value per line.",
}: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  hint?: string;
}) {
  const id = useId();
  return (
    <div className="min-w-0 space-y-1">
      <label className="block text-sm font-medium" htmlFor={id}>
        {label}
      </label>
      <Textarea
        id={id}
        value={values.join("\n")}
        onChange={(e) => onChange(e.target.value.split("\n"))}
        rows={2}
        aria-describedby={`${id}-hint`}
      />
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        {hint}
      </p>
    </div>
  );
}
export function Choice({
  label,
  value,
  onChange,
  items,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  items: readonly (readonly [string, string])[];
}) {
  const id = useId();
  return (
    <div className="min-w-0 space-y-1">
      <label id={`${id}-label`} className="block text-sm font-medium">
        {label}
      </label>
      <Select
        items={Object.fromEntries(items)}
        value={value}
        onValueChange={(next) => {
          if (next !== null) onChange(next);
        }}
      >
        <SelectTrigger aria-labelledby={`${id}-label`} className="w-full min-w-0">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {items.map(([key, text]) => (
            <SelectItem key={key} value={key}>
              {text}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
export function Advanced({ children, label = "Advanced options" }: { children: ReactNode; label?: string }) {
  return (
    <details className="rounded-md border p-3">
      <summary className="cursor-pointer text-sm font-medium">{label}</summary>
      <div className="mt-4 space-y-4">{children}</div>
    </details>
  );
}
export function RuleEditor({
  prefix,
  rule,
  onChange,
}: {
  prefix: string;
  rule: ClaimRule;
  onChange: (rule: ClaimRule) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="grid gap-3 md:grid-cols-2">
        <Field
          label={`${prefix} attribute`}
          value={rule.claim}
          onChange={(claim) => onChange({ ...rule, claim })}
          hint="For example roles, groups, or a nested field such as realm_access.roles."
          required
        />
        <ListField
          label={`${prefix} allowed values`}
          values={rule.any_of}
          onChange={(any_of) => onChange({ ...rule, any_of })}
          hint="One value per line. At least one must match."
        />
      </div>
      <Advanced label={`${prefix} advanced options`}>
        <Choice
          label={`${prefix} value format`}
          value={rule.value_format ?? "scalar_or_array"}
          onChange={(value_format) => onChange({ ...rule, value_format: value_format as ClaimRule["value_format"] })}
          items={[
            ["scalar_or_array", "A value or list of values"],
            ["space_delimited", "Values separated by spaces"],
          ]}
        />
      </Advanced>
    </div>
  );
}
export const emptyRule = (): ClaimRule => ({ claim: "", any_of: [], value_format: "scalar_or_array" });
export const cleanList = (values: string[]) => values.map((value) => value.trim()).filter(Boolean);
export const cleanRule = (rule: ClaimRule): ClaimRule => ({
  ...rule,
  claim: rule.claim.trim(),
  any_of: cleanList(rule.any_of),
});
