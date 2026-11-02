import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SSOSettings from "./SSOSettings";

vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    accessToken: "fixture-admin",
    userId: "admin",
    userRole: "proxy_admin",
    isAuthorized: true,
    isLoading: false,
    isOidcSession: false,
  }),
}));
vi.mock("@/components/networking", () => ({ getProxyBaseUrl: () => "https://gateway.example.test" }));
vi.mock("@/app/(dashboard)/hooks/sso/useSSOSettings", () => ({
  useSSOSettings: () => ({ data: null, isLoading: false, refetch: vi.fn() }),
}));

const initial = {
  revision: 0,
  enabled: false,
  source: "unset",
  read_only: false,
  read_only_reason: null,
  browser: null,
  api: null,
  client_secret: { configured: false, source: "unset", environment_variable: null },
  session_secret: { configured: false, source: "unset", environment_variable: null },
  prerequisites: { database_connected: true, master_key_configured: true, custom_auth_compatible: true },
  legacy_sso_configured: false,
  mcp_servers: [],
  recovery_login_url: "/fallback/login",
};
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
let requests: { url: string; method: string; body?: any }[];

beforeEach(() => {
  requests = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ url: String(url), method, body });
      if (method === "GET") return json(initial);
      return json({
        ...initial,
        revision: 1,
        enabled: true,
        source: "database",
        browser: body.browser,
        api: body.api,
        client_secret: { configured: true, source: "database", environment_variable: null },
        sessions_invalidated: false,
      });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

it("saves a complete independent configuration from the native SSO tab without JSON or legacy mutations", async () => {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SSOSettings />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "OpenID Connect", exact: true });
  fireEvent.change(screen.getByLabelText("Issuer URL", { exact: true }), {
    target: { value: "https://id.example.test" },
  });
  fireEvent.change(screen.getByLabelText("Application ID", { exact: true }), { target: { value: "fixture-client" } });
  fireEvent.change(screen.getByLabelText("Client secret", { exact: true }), {
    target: { value: "fixture-write-only" },
  });
  fireEvent.change(screen.getByLabelText("Callback URL", { exact: true }), {
    target: { value: "https://gateway.example.test/oidc/callback" },
  });
  fireEvent.change(screen.getByLabelText("Admission attribute", { exact: true }), { target: { value: "roles" } });
  fireEvent.change(screen.getByLabelText("Admission allowed values", { exact: true }), { target: { value: "access" } });
  await user.click(screen.getByRole("button", { name: "Save changes", exact: true }));
  await waitFor(() => expect(requests.some((r) => r.method === "PUT")).toBe(true));
  const saved = requests.find((r) => r.method === "PUT")!;
  expect(saved.url).toBe("https://gateway.example.test/oidc/settings");
  expect(saved.body).toMatchObject({
    expected_revision: 0,
    client_secret: "fixture-write-only",
    api: null,
    browser: {
      issuer: "https://id.example.test",
      client_id: "fixture-client",
      redirect_uri: "https://gateway.example.test/oidc/callback",
      access_rule: { claim: "roles", any_of: ["access"] },
      default_role: "internal_user_viewer",
    },
  });
  await screen.findByText("OpenID Connect is active.");
  expect(screen.queryByDisplayValue("fixture-write-only")).not.toBeInTheDocument();
  expect(requests.some((r) => r.url.includes("sso_settings"))).toBe(false);
});

it("shows file-owned settings without enabling any mutation controls", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      json({
        ...initial,
        source: "file",
        read_only: true,
        read_only_reason: "OIDC is owned by the deployment configuration.",
        browser: { issuer: "https://login.microsoftonline.com/11111111-2222-3333-4444-aaaaaaaaaaaa/v2.0" },
        client_secret: { configured: true, source: "environment", environment_variable: "OIDC_SECRET_REFERENCE" },
      }),
    ),
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SSOSettings />
    </QueryClientProvider>,
  );
  await screen.findByText("OIDC is owned by the deployment configuration.");
  expect(screen.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
  expect(screen.getByLabelText("Issuer URL", { exact: true })).toBeDisabled();
  expect(screen.getByLabelText("Directory (tenant) ID")).toHaveValue("11111111-2222-3333-4444-aaaaaaaaaaaa");
  expect(screen.getByRole("button", { name: "Apply Entra defaults" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Replace client secret", exact: true })).toBeDisabled();
  expect(screen.queryByLabelText("Client secret", { exact: true })).not.toBeInTheDocument();
  expect(screen.getByText(/OIDC_SECRET_REFERENCE/)).toBeInTheDocument();
});

it("applies Entra defaults explicitly while retaining saved credentials, roles and API access", async () => {
  const user = userEvent.setup();
  const existingFetch = globalThis.fetch;
  const browser = {
    issuer: "https://existing.example.test",
    client_id: "keep-client",
    redirect_uri: "https://gateway.example.test/oidc/callback",
    subject_claim: "custom_subject",
    scopes: ["openid", "custom"],
    access_rule: { claim: "groups", any_of: ["allowed"] },
    role_mappings: [{ rule: { claim: "groups", any_of: ["admins"] }, role: "proxy_admin" }],
    default_role: "internal_user_viewer",
  };
  const api = {
    resource: "https://gateway.example.test",
    audience: "keep-audience",
    scope_claim: "scp",
    scope_format: "space_delimited",
    token_types: ["JWT"],
    scopes: [{ name: "api://kept/access", token_value: "access" }],
    required_claims: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (!init?.method || init.method === "GET")
        return json({
          ...initial,
          revision: 7,
          source: "database",
          enabled: true,
          browser,
          api,
          client_secret: { configured: true, source: "database", environment_variable: null },
        });
      return existingFetch(url, init);
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SSOSettings />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "OpenID Connect", exact: true });
  await user.click(screen.getByRole("combobox", { name: "Provider preset" }));
  await user.click(await screen.findByRole("option", { name: "Microsoft Entra ID" }));
  const issuer = screen.getByLabelText("Issuer URL", { exact: true });
  expect(issuer).toHaveValue(browser.issuer);
  fireEvent.change(screen.getByLabelText("Directory (tenant) ID"), { target: { value: "common" } });
  expect(screen.getByRole("button", { name: "Apply Entra defaults" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Directory (tenant) ID"), {
    target: { value: " 11111111-2222-3333-4444-AAAAAAAAAAAA " },
  });
  expect(issuer).toHaveValue(browser.issuer);
  await user.click(screen.getByRole("button", { name: "Apply Entra defaults" }));
  const entraIssuer = "https://login.microsoftonline.com/11111111-2222-3333-4444-aaaaaaaaaaaa/v2.0";
  expect(issuer).toHaveValue(entraIssuer);
  await user.click(screen.getByText("Provider advanced options", { exact: true }));
  expect(screen.getByLabelText("User ID attribute", { exact: true })).toHaveValue("oid");
  expect(screen.getByLabelText("Requested sign-in scopes", { exact: true })).toHaveValue("openid\nprofile\nemail");
  const copy = vi.spyOn(navigator.clipboard, "writeText");
  await user.click(screen.getByRole("button", { name: "Copy callback URL" }));
  expect(copy).toHaveBeenCalledWith(browser.redirect_uri);
  await user.click(screen.getByRole("combobox", { name: "Provider preset" }));
  await user.click(await screen.findByRole("option", { name: "Generic OpenID Connect" }));
  expect(issuer).toHaveValue(entraIssuer);
  expect(requests.some((r) => r.method === "PUT")).toBe(false);
  await user.click(screen.getByRole("button", { name: "Save changes", exact: true }));
  await waitFor(() => expect(requests.some((r) => r.method === "PUT")).toBe(true));
  const saved = requests.find((r) => r.method === "PUT")!.body;
  expect(saved).toMatchObject({
    expected_revision: 7,
    browser: { ...browser, issuer: entraIssuer, subject_claim: "oid", scopes: ["openid", "profile", "email"] },
    api,
  });
  expect(saved).not.toHaveProperty("client_secret");
});
