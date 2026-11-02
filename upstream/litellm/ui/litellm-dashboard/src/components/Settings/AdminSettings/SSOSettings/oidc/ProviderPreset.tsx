import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Choice, Field } from "./Fields";

const tenantIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function ProviderPreset({ issuer, onApply }: { issuer: string; onApply: (issuer: string) => void }) {
  const savedTenant = issuer.match(/^https:\/\/login\.microsoftonline\.com\/([^/]+)\/v2\.0\/?$/i)?.[1] ?? "";
  const [preset, setPreset] = useState(tenantIdPattern.test(savedTenant) ? "entra" : "generic");
  const [tenantId, setTenantId] = useState(tenantIdPattern.test(savedTenant) ? savedTenant : "");
  const tenant = tenantId.trim().toLowerCase();

  return (
    <div className="space-y-3">
      <Choice
        label="Provider preset"
        value={preset}
        onChange={setPreset}
        items={[
          ["generic", "Generic OpenID Connect"],
          ["entra", "Microsoft Entra ID"],
        ]}
      />
      {preset === "entra" && (
        <div className="space-y-3 rounded-md border p-3">
          <Field
            label="Directory (tenant) ID"
            value={tenantId}
            onChange={setTenantId}
            hint="Copy the tenant ID (a GUID) from your Entra app registration. This preset uses Microsoft's public cloud."
          />
          <p className="text-sm text-muted-foreground">
            Sets the tenant issuer, user ID attribute to oid, and sign-in scopes to openid, profile and email. Client
            credentials, callback, access rules and API settings stay as entered. Review the values before saving.
          </p>
          <Button
            type="button"
            variant="outline"
            disabled={!tenantIdPattern.test(tenant)}
            onClick={() => onApply(`https://login.microsoftonline.com/${tenant}/v2.0`)}
          >
            Apply Entra defaults
          </Button>
        </div>
      )}
    </div>
  );
}
