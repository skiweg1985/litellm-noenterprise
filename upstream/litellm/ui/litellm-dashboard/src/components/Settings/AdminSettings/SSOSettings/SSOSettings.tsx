"use client";

import { useEffect, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { isProxyAdminRole } from "@/utils/roles";
import { Button } from "@/components/ui/button";
import { getOIDCSettings, type SettingsResponse, type SaveSettingsResponse } from "@/lib/http/oidcSettings";
import LegacySSOSettings from "./LegacySSOSettings";
import { OIDCSettingsForm } from "./oidc/OIDCSettingsForm";

function LegacySettings({ active }: { active: boolean }) {
  const [open, setOpen] = useState(active);
  return (
    <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="rounded-lg border p-4">
      <summary className="cursor-pointer text-sm font-medium">
        {active ? "Current legacy SSO settings" : "Preserved legacy SSO settings"}
      </summary>
      <p className="my-3 text-sm text-muted-foreground">
        {active
          ? "Your existing SSO configuration remains in use until OpenID Connect is enabled."
          : "These settings are preserved. Independent OpenID Connect takes precedence while enabled."}
      </p>
      {open && <LegacySSOSettings />}
    </details>
  );
}

function Settings({ accessToken, isOidcSession }: { accessToken: string; isOidcSession: boolean }) {
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [editIndependent, setEditIndependent] = useState(false);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  function saved(result: SaveSettingsResponse) {
    setSettings(result);
    if (isOidcSession && result.sessions_invalidated) setNeedsSignIn(true);
  }
  useEffect(() => {
    const controller = new AbortController();
    getOIDCSettings(accessToken, controller.signal)
      .then(setSettings)
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => controller.abort();
  }, [accessToken, reload]);
  if (failed)
    return (
      <div className="space-y-3" role="alert">
        <p>Sign-in settings could not be loaded. Try again to check the current configuration.</p>
        <Button
          onClick={() => {
            setFailed(false);
            setReload((value) => value + 1);
          }}
        >
          Try again
        </Button>
      </div>
    );
  if (needsSignIn)
    return (
      <div role="alert" className="space-y-3">
        <p>Your settings were saved. Your current sign-in has ended.</p>
        <a
          className="underline"
          href={settings?.enabled ? "/oidc/login" : settings?.recovery_login_url ?? "/fallback/login"}
        >
          {settings?.enabled ? "Sign in again" : "Sign in with a native administrator account"}
        </a>
        <p>
          <a className="underline" href="/fallback/login">
            Native administrator recovery login
          </a>
        </p>
      </div>
    );
  if (!settings) return <p role="status">Loading sign-in settings…</p>;
  const legacyActive = settings.legacy_sso_configured && !settings.enabled;
  const showForm = !legacyActive || editIndependent || settings.source === "file";
  return (
    <div className="space-y-6">
      {legacyActive && <LegacySettings active />}
      {showForm ? (
        <OIDCSettingsForm
          key={settings.revision}
          settings={settings}
          accessToken={accessToken}
          onSaved={saved}
          onReload={() => {
            setSettings(null);
            setReload((value) => value + 1);
          }}
        />
      ) : (
        <div className="space-y-3">
          <h2 className="text-lg font-semibold">OpenID Connect</h2>
          <p className="text-sm text-muted-foreground">
            Configure independent OpenID Connect for sign-in, application access and personal MCP permissions. Your
            existing SSO settings will be retained.
          </p>
          <Button onClick={() => setEditIndependent(true)}>Configure OpenID Connect</Button>
        </div>
      )}
      {settings.legacy_sso_configured && !legacyActive && <LegacySettings active={false} />}
    </div>
  );
}

export default function SSOSettings() {
  const { accessToken, isLoading, isAuthorized, userRole, isOidcSession } = useAuthorized();
  if (isLoading) return <p role="status">Checking your session…</p>;
  if (!isAuthorized || !accessToken || !isProxyAdminRole(userRole ?? ""))
    return <p role="alert">Administrator access is required to manage sign-in settings.</p>;
  return <Settings accessToken={accessToken} isOidcSession={isOidcSession} />;
}
