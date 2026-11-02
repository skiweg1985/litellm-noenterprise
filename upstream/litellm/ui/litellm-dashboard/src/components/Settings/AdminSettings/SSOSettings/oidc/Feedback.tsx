import { ApiError } from "@/lib/http/client";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogFooter,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import type { components } from "@/lib/http/oidc-settings-schema";

type Detail = components["schemas"]["SettingsError"]["detail"];
const labels: Record<string, string> = {
  browser: "Sign-in",
  api: "API access",
  access_rule: "Admission",
  role_mappings: "Role mapping",
  default_role: "Default role",
  mcp_access_rules: "MCP rule",
  issuer: "Issuer URL",
  client_id: "Application ID",
  client_secret: "Client secret",
  redirect_uri: "Callback URL",
  any_of: "Allowed values",
  claim: "Attribute",
  scopes: "Scopes",
  scope_claim: "Scope attribute",
  scope_format: "Scope format",
  resource: "Public API address",
  audience: "API audience",
  required_claims: "API requirement",
  token_types: "Access-token types",
  session_ttl_seconds: "Session lifetime",
  subject_claim: "User ID attribute",
  retained_claims: "Retained attributes",
  name: "Public name",
  token_value: "Token value",
  value_format: "Value format",
  role: "Role",
};
export function settingsError(error: unknown): Detail {
  if (error instanceof ApiError && error.body && typeof error.body === "object" && "detail" in error.body) {
    const detail = error.body.detail;
    if (
      detail &&
      typeof detail === "object" &&
      "code" in detail &&
      "message" in detail &&
      typeof detail.message === "string" &&
      "errors" in detail &&
      Array.isArray(detail.errors)
    )
      return detail as Detail;
  }
  return {
    code: "configuration_unavailable",
    message:
      "The server response could not be confirmed. Your edits are still here. Reload saved settings to check the active configuration.",
    errors: [],
  };
}
export function ErrorFeedback({ error, reload }: { error: Detail | null; reload: () => void }) {
  if (!error) return null;
  return (
    <div role="alert" className="space-y-2 rounded border border-destructive p-3 text-sm">
      <p>
        {error.code === "revision_conflict"
          ? "Settings changed elsewhere. Reload the saved settings before trying again."
          : error.message}
      </p>
      {!!error.errors?.length && (
        <ul className="list-disc space-y-1 pl-5">
          {error.errors.map((field, index) => (
            <li key={index}>
              {field.path
                .map((part) => (typeof part === "number" ? String(part + 1) : labels[part] ?? part))
                .join(" → ")}
              : {field.message}
            </li>
          ))}
        </ul>
      )}
      {(error.code === "revision_conflict" ||
        error.code === "file_managed" ||
        error.code === "configuration_unavailable") && (
        <Button type="button" variant="outline" onClick={reload}>
          Reload saved settings
        </Button>
      )}
    </div>
  );
}
export function Confirmation({
  kind,
  close,
  confirm,
  pending,
}: {
  kind: "disable" | "reload" | null;
  close: () => void;
  confirm: () => void;
  pending: boolean;
}) {
  const action = kind === "disable" ? "Confirm disable" : "Discard edits and reload";
  return (
    <AlertDialog
      open={kind !== null}
      onOpenChange={(open) => {
        if (!open && !pending) close();
      }}
    >
      <AlertDialogContent>
        <AlertDialogTitle>{kind === "disable" ? "Disable OpenID Connect?" : "Discard unsaved edits?"}</AlertDialogTitle>
        <AlertDialogDescription>
          {kind === "disable"
            ? "Independent OpenID Connect sessions will stop working. Native administrator recovery remains available, and any configured legacy SSO takes over. Users, MCP connections and saved settings are retained. Unsaved form edits will be discarded."
            : "Your unsaved form changes will be discarded and replaced with the latest saved settings."}
        </AlertDialogDescription>
        <AlertDialogFooter>
          <Button type="button" variant="outline" disabled={pending} onClick={close}>
            Cancel
          </Button>
          <Button type="button" disabled={pending} onClick={confirm}>
            {pending ? "Please wait…" : action}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
