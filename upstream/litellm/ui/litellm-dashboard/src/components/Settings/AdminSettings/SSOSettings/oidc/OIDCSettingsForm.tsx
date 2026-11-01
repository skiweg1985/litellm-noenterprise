import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  saveOIDCSettings,
  disableOIDCSettings,
  checkOIDCProvider,
  type SettingsResponse,
  type SaveSettingsResponse,
  type CheckProviderResponse,
} from "@/lib/http/oidcSettings";
import { browserDefaults, apiDefaults } from "./defaults";
import { Advanced, Field, ListField, RuleEditor, Section, cleanList, cleanRule } from "./Fields";
import { APISection, MCPSection, RolesSection } from "./PolicySections";
import { Confirmation, ErrorFeedback, settingsError } from "./Feedback";

export function OIDCSettingsForm({
  settings,
  accessToken,
  onSaved,
  onReload,
}: {
  settings: SettingsResponse;
  accessToken: string;
  onSaved: (settings: SaveSettingsResponse) => void;
  onReload: () => void;
}) {
  const [browser, setBrowser] = useState(() => browserDefaults(settings.browser ?? null));
  const [api, setApi] = useState(() => apiDefaults(settings.api ?? null));
  const [apiEnabled, setApiEnabled] = useState(!!settings.api);
  const [secret, setSecret] = useState("");
  const [replaceSecret, setReplaceSecret] = useState(!settings.client_secret?.configured);
  const [pending, setPending] = useState(false);
  const [checking, setChecking] = useState(false);
  const [provider, setProvider] = useState<CheckProviderResponse | null>(null);
  const [error, setError] = useState<ReturnType<typeof settingsError> | null>(null);
  const [confirmation, setConfirmation] = useState<"disable" | "reload" | null>(null);
  const [copied, setCopied] = useState(false);
  const update = <K extends keyof typeof browser>(key: K, value: (typeof browser)[K]) =>
    setBrowser((current) => ({ ...current, [key]: value }));
  const prerequisites = settings.prerequisites;
  const unavailable =
    !prerequisites.database_connected || !prerequisites.master_key_configured || !prerequisites.custom_auth_compatible;
  async function save(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const result = await saveOIDCSettings(accessToken, {
        expected_revision: settings.revision ?? 0,
        browser: {
          ...browser,
          issuer: browser.issuer.trim(),
          client_id: browser.client_id.trim(),
          redirect_uri: browser.redirect_uri.trim(),
          subject_claim: browser.subject_claim.trim(),
          scopes: cleanList(browser.scopes),
          retained_claims: cleanList(browser.retained_claims),
          access_rule: cleanRule(browser.access_rule),
          role_mappings: browser.role_mappings.map((mapping) => ({ ...mapping, rule: cleanRule(mapping.rule) })),
          mcp_access_rules: Object.fromEntries(
            Object.entries(browser.mcp_access_rules).map(([id, rule]) => [id, cleanRule(rule)]),
          ),
        },
        api: apiEnabled
          ? {
              ...api,
              resource: api.resource.trim(),
              audience: api.audience.trim(),
              scope_claim: api.scope_claim.trim(),
              scopes: api.scopes.map((scope) => ({ name: scope.name.trim(), token_value: scope.token_value.trim() })),
              required_claims: api.required_claims.map(cleanRule),
            }
          : null,
        ...(replaceSecret && secret ? { client_secret: secret } : {}),
      });
      setSecret("");
      setReplaceSecret(false);
      onSaved(result);
    } catch (failure) {
      setError(settingsError(failure));
    } finally {
      setPending(false);
    }
  }
  async function check() {
    setChecking(true);
    setProvider(null);
    setError(null);
    try {
      setProvider(await checkOIDCProvider(accessToken, browser.issuer.trim(), browser.allow_insecure_http));
    } catch (failure) {
      setError(settingsError(failure));
    } finally {
      setChecking(false);
    }
  }
  async function confirm() {
    if (confirmation === "reload") {
      setSecret("");
      onReload();
      return;
    }
    setPending(true);
    setError(null);
    try {
      const result = await disableOIDCSettings(accessToken, settings.revision ?? 0);
      setSecret("");
      setConfirmation(null);
      onSaved(result);
    } catch (failure) {
      setConfirmation(null);
      setError(settingsError(failure));
    } finally {
      setPending(false);
    }
  }
  let status = "OpenID Connect is disabled. Saving enables it.";
  if (settings.source === "unset") status = "OpenID Connect is not configured. Saving enables it.";
  if (settings.read_only) status = "OpenID Connect is disabled in the deployment configuration.";
  if (settings.enabled) status = "OpenID Connect is active.";
  const sources = { file: "deployment file", database: "saved in LiteLLM", unset: "not configured" };
  return (
    <section className="space-y-5" aria-label="Independent OpenID Connect settings">
      <div>
        <h2 className="text-lg font-semibold">OpenID Connect</h2>
        <p className="text-sm text-muted-foreground">Manage sign-in and access using your identity provider.</p>
      </div>
      <p role="status">{status}</p>
      <p className="text-xs text-muted-foreground">Configuration source: {sources[settings.source ?? "unset"]}.</p>
      {settings.read_only && (
        <p role="note" className="rounded border p-3 text-sm">
          {settings.read_only_reason ||
            "These settings are managed by your deployment file and cannot be changed here."}
        </p>
      )}
      {unavailable && (
        <div role="note" className="rounded border p-3 text-sm">
          <p>Deployment setup is required before changes can be saved:</p>
          <ul className="list-disc pl-5">
            {!prerequisites.database_connected && <li>Connect the LiteLLM database.</li>}
            {!prerequisites.master_key_configured && <li>Configure the LiteLLM master key.</li>}
            {!prerequisites.custom_auth_compatible && <li>Remove the conflicting custom authentication handler.</li>}
          </ul>
        </div>
      )}
      <form onSubmit={save} className="space-y-5">
        <fieldset disabled={settings.read_only || pending || checking} className="min-w-0 space-y-5">
          <Section title="Sign-in provider">
            <div className="grid gap-4 md:grid-cols-2">
              <Field
                label="Issuer URL"
                value={browser.issuer}
                onChange={(value) => {
                  update("issuer", value);
                  setProvider(null);
                }}
                hint="The issuer address from your provider's OpenID Connect configuration."
                required
              />
              <Field
                label="Application ID"
                value={browser.client_id}
                onChange={(value) => update("client_id", value)}
                hint="Also called the client ID."
                required
              />
            </div>
            <Field
              label="Callback URL"
              value={browser.redirect_uri}
              onChange={(value) => {
                update("redirect_uri", value);
                setCopied(false);
              }}
              hint="Register this exact callback URL in your provider's application."
              required
            />
            <Button
              type="button"
              variant="outline"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(browser.redirect_uri);
                  setCopied(true);
                } catch {
                  setCopied(false);
                }
              }}
            >
              {copied ? "Callback copied" : "Copy callback URL"}
            </Button>
            <p className="text-sm">
              Client secret: {settings.client_secret?.configured ? "Configured" : "Not configured"}
              {settings.client_secret?.environment_variable ? ` (${settings.client_secret?.environment_variable})` : ""}
              .
            </p>
            {replaceSecret ? (
              <>
                <Field
                  label="Client secret"
                  value={secret}
                  onChange={setSecret}
                  type="password"
                  required
                  hint="Stored securely. The saved value is never returned to this page."
                />
                {settings.client_secret?.configured && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      setSecret("");
                      setReplaceSecret(false);
                    }}
                  >
                    Keep existing secret
                  </Button>
                )}
              </>
            ) : (
              <Button type="button" variant="outline" onClick={() => setReplaceSecret(true)}>
                Replace client secret
              </Button>
            )}
            <Advanced label="Provider advanced options">
              <ListField
                label="Requested sign-in scopes"
                values={browser.scopes}
                onChange={(value) => update("scopes", value)}
                hint="One per line. Keep openid. These are requested during browser sign-in."
              />
              <Field
                label="User ID attribute"
                value={browser.subject_claim}
                onChange={(value) => update("subject_claim", value)}
                required
                hint="Use a stable provider user identifier, normally sub."
              />
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={browser.allow_insecure_http}
                  onChange={(event) => {
                    update("allow_insecure_http", event.target.checked);
                    setProvider(null);
                  }}
                />
                Allow HTTP for local testing only
              </label>
            </Advanced>
            <Button type="button" variant="outline" disabled={!browser.issuer.trim()} onClick={check}>
              {checking ? "Checking…" : "Check provider"}
            </Button>
            {provider && (
              <div className="space-y-3 text-sm">
                <p role="status">Discovery is valid. The client secret and user sign-in have not been tested.</p>
                <Advanced label="Discovered provider details">
                  <dl className="space-y-2 break-words">
                    {[
                      ["Issuer", provider.issuer],
                      ["Authorization endpoint", provider.authorization_endpoint],
                      ["Token endpoint", provider.token_endpoint],
                      ["Signing keys", provider.jwks_uri],
                      ["Available scopes", provider.scopes_supported.join(", ") || "Not advertised"],
                      ["PKCE methods", provider.code_challenge_methods_supported?.join(", ") || "Not advertised"],
                      ["Client authentication", provider.token_endpoint_auth_methods_supported.join(", ")],
                      ["Signing algorithms", provider.id_token_signing_alg_values_supported.join(", ")],
                    ].map(([label, value]) => (
                      <div key={label}>
                        <dt className="font-medium">{label}</dt>
                        <dd>{value}</dd>
                      </div>
                    ))}
                  </dl>
                </Advanced>
              </div>
            )}
          </Section>
          <Section title="Who may sign in">
            <p className="text-sm text-muted-foreground">
              This rule permits both the creation of a new account and every later sign-in.
            </p>
            <RuleEditor
              prefix="Admission"
              rule={browser.access_rule}
              onChange={(rule) => update("access_rule", rule)}
            />
          </Section>
          <RolesSection value={browser} onChange={setBrowser} />
          <APISection enabled={apiEnabled} setEnabled={setApiEnabled} value={api} onChange={setApi} />
          <MCPSection value={browser} onChange={setBrowser} servers={settings.mcp_servers ?? []} />
          <Section title="Sessions">
            <Field
              label="Session lifetime in seconds"
              value={String(browser.session_ttl_seconds)}
              type="number"
              onChange={(value) => update("session_ttl_seconds", Number(value))}
              hint="Between 60 and 86400 seconds. Changes to identity or access rules require existing OIDC users to sign in again."
              required
            />
            <Advanced label="Session advanced options">
              <ListField
                label="Retained sign-in attributes"
                values={browser.retained_claims}
                onChange={(value) => update("retained_claims", value)}
                hint="One attribute per line. Keep attributes used by MCP and access rules."
              />
              <p className="text-sm">
                Session key: {settings.session_secret?.configured ? "Configured" : "Created securely on first save"}
                {settings.session_secret?.environment_variable
                  ? ` (${settings.session_secret?.environment_variable})`
                  : ""}
                . Ordinary saves keep the existing key.
              </p>
            </Advanced>
          </Section>
          <p className="text-sm text-muted-foreground">
            Save changes validates and enables the complete configuration immediately. A provider check does not test a
            real user sign-in.
          </p>
          <div className="flex flex-wrap gap-3">
            <Button type="submit" disabled={unavailable}>
              {pending ? "Saving…" : "Save changes"}
            </Button>
            {settings.enabled && (
              <Button type="button" variant="outline" onClick={() => setConfirmation("disable")}>
                Disable OpenID Connect
              </Button>
            )}
          </div>
        </fieldset>
        <ErrorFeedback error={error} reload={() => setConfirmation("reload")} />
      </form>
      <p className="text-sm">
        <a className="underline" href={settings.recovery_login_url}>
          Native administrator recovery login
        </a>
      </p>
      <Confirmation kind={confirmation} close={() => setConfirmation(null)} confirm={confirm} pending={pending} />
    </section>
  );
}
