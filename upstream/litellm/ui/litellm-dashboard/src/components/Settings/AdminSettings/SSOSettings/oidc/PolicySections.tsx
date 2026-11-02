import { Button } from "@/components/ui/button";
import type { APISettings, BrowserSettings, SettingsResponse } from "@/lib/http/oidcSettings";
import { Advanced, Choice, Field, RuleEditor, Section, emptyRule } from "./Fields";
import { roleChoices, defaultRoleChoices } from "./defaults";

type Browser = Required<BrowserSettings>;
export function RolesSection({ value, onChange }: { value: Browser; onChange: (value: Browser) => void }) {
  const mappings = value.role_mappings;
  function move(index: number, direction: number) {
    const next = [...mappings];
    [next[index], next[index + direction]] = [next[index + direction], next[index]];
    onChange({ ...value, role_mappings: next });
  }
  return (
    <Section title="Roles after sign-in">
      <p className="text-sm text-muted-foreground">
        Rules are checked from top to bottom. The first match determines the user&apos;s role at every sign-in.
      </p>
      {mappings.map((mapping, index) => (
        <div key={index} className="space-y-3 rounded border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-sm font-medium">Mapping {index + 1}</h4>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={index === 0}
                aria-label={`Move mapping ${index + 1} up`}
                onClick={() => move(index, -1)}
              >
                Move up
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={index === mappings.length - 1}
                aria-label={`Move mapping ${index + 1} down`}
                onClick={() => move(index, 1)}
              >
                Move down
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                aria-label={`Remove mapping ${index + 1}`}
                onClick={() => onChange({ ...value, role_mappings: mappings.filter((_, i) => i !== index) })}
              >
                Remove
              </Button>
            </div>
          </div>
          <RuleEditor
            prefix={`Mapping ${index + 1}`}
            rule={mapping.rule}
            onChange={(rule) =>
              onChange({ ...value, role_mappings: mappings.map((row, i) => (i === index ? { ...row, rule } : row)) })
            }
          />
          <Choice
            label={`Mapping ${index + 1} role`}
            value={mapping.role}
            items={roleChoices}
            onChange={(role) =>
              onChange({
                ...value,
                role_mappings: mappings.map((row, i) =>
                  i === index ? { ...row, role: role as typeof mapping.role } : row,
                ),
              })
            }
          />
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        onClick={() =>
          onChange({ ...value, role_mappings: [...mappings, { rule: emptyRule(), role: "internal_user_viewer" }] })
        }
      >
        Add role mapping
      </Button>
      <Choice
        label="Default role"
        value={value.default_role}
        items={defaultRoleChoices}
        onChange={(default_role) => onChange({ ...value, default_role: default_role as Browser["default_role"] })}
      />
      <p className="text-xs text-muted-foreground">
        Used only after the admission rule passes and no role mapping matches.
      </p>
    </Section>
  );
}

export function APISection({
  enabled,
  setEnabled,
  value,
  onChange,
}: {
  enabled: boolean;
  setEnabled: (value: boolean) => void;
  value: Required<APISettings>;
  onChange: (value: Required<APISettings>) => void;
}) {
  const update = <K extends keyof typeof value>(key: K, next: (typeof value)[K]) => onChange({ ...value, [key]: next });
  return (
    <Section title="Application and API access">
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        Allow applications to use provider access tokens
      </label>
      <p className="text-sm text-muted-foreground">
        Applications authenticate the user directly with your provider. This does not grant administrator access or
        replace a personal MCP authorization.
      </p>
      {enabled && (
        <div className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <Field
              label="Public API address"
              value={value.resource}
              onChange={(resource) => update("resource", resource)}
              required
              hint="The public LiteLLM origin, without a path."
            />
            <Field
              label="API audience"
              value={value.audience}
              onChange={(audience) => update("audience", audience)}
              required
              hint="The resource identifier the provider puts in access tokens."
            />
          </div>
          <p className="text-sm">
            Every scope below is required. Public names appear in the discovery metadata; token values must match the
            provider&apos;s token.
          </p>
          {value.scopes.map((scope, index) => (
            <div className="space-y-3 rounded border p-3" key={index}>
              <div className="grid gap-3 md:grid-cols-2">
                <Field
                  label={`Scope ${index + 1} public name`}
                  value={scope.name}
                  onChange={(name) =>
                    update(
                      "scopes",
                      value.scopes.map((row, i) => (i === index ? { ...row, name } : row)),
                    )
                  }
                  required
                />
                <Field
                  label={`Scope ${index + 1} token value`}
                  value={scope.token_value}
                  onChange={(token_value) =>
                    update(
                      "scopes",
                      value.scopes.map((row, i) => (i === index ? { ...row, token_value } : row)),
                    )
                  }
                  required
                />
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                aria-label={`Remove scope ${index + 1}`}
                disabled={value.scopes.length === 1}
                onClick={() =>
                  update(
                    "scopes",
                    value.scopes.filter((_, i) => i !== index),
                  )
                }
              >
                Remove
              </Button>
            </div>
          ))}
          <Button
            type="button"
            variant="outline"
            onClick={() => update("scopes", [...value.scopes, { name: "", token_value: "" }])}
          >
            Add required scope
          </Button>
          <Advanced label="API advanced options">
            <Field
              label="Scope attribute"
              value={value.scope_claim}
              onChange={(scope_claim) => update("scope_claim", scope_claim)}
              required
            />
            <Choice
              label="Scope value format"
              value={value.scope_format}
              items={[
                ["space_delimited", "Values separated by spaces"],
                ["array", "A list of values"],
              ]}
              onChange={(scope_format) => update("scope_format", scope_format as typeof value.scope_format)}
            />
            <fieldset className="space-y-2">
              <legend className="mb-2 text-sm font-medium">Accepted access-token types</legend>
              {(
                [
                  ["at+jwt", "JWT access tokens (at+jwt)"],
                  ["application/at+jwt", "JWT access tokens (application/at+jwt)"],
                  ["JWT", "Generic JWT access tokens"],
                ] as const
              ).map(([type, label]) => (
                <label key={type} className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={value.token_types.includes(type)}
                    onChange={(event) =>
                      update(
                        "token_types",
                        event.target.checked
                          ? [...value.token_types, type]
                          : value.token_types.filter((item) => item !== type),
                      )
                    }
                  />
                  {label}
                </label>
              ))}
            </fieldset>
            <p className="text-xs text-muted-foreground">
              Generic JWT tokens require an API audience different from the sign-in application ID. ID tokens are not
              accepted as API credentials.
            </p>
            {value.required_claims.map((rule, index) => (
              <div className="space-y-3 rounded border p-3" key={index}>
                <RuleEditor
                  prefix={`API rule ${index + 1}`}
                  rule={rule}
                  onChange={(next) =>
                    update(
                      "required_claims",
                      value.required_claims.map((item, i) => (i === index ? next : item)),
                    )
                  }
                />
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  aria-label={`Remove API rule ${index + 1}`}
                  onClick={() =>
                    update(
                      "required_claims",
                      value.required_claims.filter((_, i) => i !== index),
                    )
                  }
                >
                  Remove
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              onClick={() => update("required_claims", [...value.required_claims, emptyRule()])}
            >
              Add API requirement
            </Button>
            <p className="text-xs text-muted-foreground">All additional API requirements must match.</p>
          </Advanced>
        </div>
      )}
    </Section>
  );
}

export function MCPSection({
  value,
  onChange,
  servers,
}: {
  value: Browser;
  onChange: (value: Browser) => void;
  servers: NonNullable<SettingsResponse["mcp_servers"]>;
}) {
  const rows = Object.entries(value.mcp_access_rules);
  const available = servers.filter((server) => !(server.server_id in value.mcp_access_rules));
  return (
    <Section title="Personal MCP connections">
      <p className="text-sm text-muted-foreground">
        Add a sign-in attribute requirement for each MCP server. Existing server permissions still apply. A server
        without a rule keeps its existing permissions.
      </p>
      {rows.map(([key, rule], index) => {
        const known = servers.find((server) => server.server_id === key);
        const items: [string, string][] = servers
          .filter((server) => server.server_id === key || !(server.server_id in value.mcp_access_rules))
          .map((server) => [server.server_id, server.name]);
        if (!known) items.unshift([key, `Saved reference: ${key}`]);
        return (
          <div className="space-y-3 rounded border p-3" key={index}>
            <Choice
              label={`MCP rule ${index + 1} server`}
              value={key}
              items={items}
              onChange={(newKey) =>
                onChange({
                  ...value,
                  mcp_access_rules: Object.fromEntries(
                    rows.map(([id, current]) => [id === key ? newKey : id, current]),
                  ),
                })
              }
            />
            {!known && (
              <p className="text-xs text-muted-foreground">
                This saved name or alias is retained. Select a server to use its stable ID, or remove the rule
                explicitly.
              </p>
            )}
            <RuleEditor
              prefix={`MCP rule ${index + 1}`}
              rule={rule}
              onChange={(next) => onChange({ ...value, mcp_access_rules: { ...value.mcp_access_rules, [key]: next } })}
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={`Remove MCP rule ${index + 1}`}
              onClick={() =>
                onChange({ ...value, mcp_access_rules: Object.fromEntries(rows.filter(([id]) => id !== key)) })
              }
            >
              Remove
            </Button>
          </div>
        );
      })}
      <Button
        type="button"
        variant="outline"
        disabled={!available.length}
        onClick={() => {
          if (available[0])
            onChange({
              ...value,
              mcp_access_rules: { ...value.mcp_access_rules, [available[0].server_id]: emptyRule() },
            });
        }}
      >
        Add MCP access rule
      </Button>
      {!servers.length && (
        <p className="text-xs text-muted-foreground">
          Add MCP servers in the MCP Servers section before choosing one here.
        </p>
      )}
    </Section>
  );
}
