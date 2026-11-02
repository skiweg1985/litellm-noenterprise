"""Independent OIDC management through public HTTP and the controlled issuer."""

import copy
import os
import unittest

import httpx

BASE = "http://proxy:4000"
MASTER = os.environ.get("EXPECTED_MASTER_KEY", "sk-identity-test-only")


@unittest.skipUnless(
    os.environ.get("OIDC_SETTINGS_MODE") == "true",
    "requires database-owned OIDC fixture",
)
class OIDCSettingsTests(unittest.TestCase):
    def test_00_unset_configuration_is_admin_only_and_has_no_secrets(self):
        response = httpx.get(
            BASE + "/oidc/settings",
            headers={"Authorization": "Bearer " + MASTER},
            trust_env=False,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "unset")
        self.assertFalse(body["enabled"])
        self.assertFalse(body["client_secret"]["configured"])
        self.assertEqual(body["revision"], 0)
        self.assertNotIn("fixture-only", response.text)
        self.assertIn(
            httpx.get(BASE + "/oidc/settings", trust_env=False).status_code, (401, 403)
        )

    def test_save_enables_real_login_and_provider_api_without_exposing_secrets(self):
        admin = {"Authorization": "Bearer " + MASTER}
        current = httpx.get(
            BASE + "/oidc/settings", headers=admin, trust_env=False
        ).json()
        browser = {
            "issuer": "http://issuer:8080",
            "client_id": "litellm-test",
            "redirect_uri": BASE + "/oidc/callback",
            "allow_insecure_http": True,
            "subject_claim": "oid",
            "access_rule": {"claim": "roles", "any_of": ["access"]},
            "role_mappings": [
                {"rule": {"claim": "roles", "any_of": ["admin"]}, "role": "proxy_admin"}
            ],
        }
        api = {
            "resource": BASE,
            "audience": "litellm-api",
            "scope_claim": "scp",
            "token_types": ["JWT"],
            "scopes": [{"name": "api://fixture/access", "token_value": "access"}],
        }
        saved = httpx.put(
            BASE + "/oidc/settings",
            headers=admin,
            json={
                "expected_revision": current["revision"],
                "browser": browser,
                "api": api,
                "client_secret": "fixture-only",
            },
            trust_env=False,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["enabled"])
        self.assertEqual(saved.json()["source"], "database")
        self.assertNotIn("fixture-only", saved.text)
        self.assertTrue(saved.json()["client_secret"]["configured"])
        self.assertTrue(saved.json()["session_secret"]["configured"])
        with httpx.Client(
            base_url=BASE, trust_env=False, follow_redirects=False
        ) as client:
            start = client.get("/sso/key/generate")
            self.assertEqual(start.status_code, 302, start.text)
            authorized = client.get(start.headers["location"])
            complete = client.get(authorized.headers["location"])
            self.assertEqual(complete.status_code, 303, complete.text)
            self.assertEqual(client.get("/oidc/session").status_code, 200)
        token = httpx.post(
            "http://issuer:8080/control/access-token", json={}, trust_env=False
        ).json()["access_token"]
        accepted = httpx.get(
            BASE + "/v1/models",
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        ui = httpx.get(BASE + "/.well-known/litellm-ui-config", trust_env=False)
        self.assertTrue(ui.json()["sso_configured"], ui.text)
        metadata = httpx.get(
            BASE + "/.well-known/oauth-protected-resource", trust_env=False
        ).json()
        self.assertEqual(metadata["scopes_supported"], ["api://fixture/access"])

    def enabled(self):
        admin = {"Authorization": "Bearer " + MASTER}
        current = httpx.get(
            BASE + "/oidc/settings", headers=admin, trust_env=False
        ).json()
        browser = {
            "issuer": "http://issuer:8080",
            "client_id": "litellm-test",
            "redirect_uri": BASE + "/oidc/callback",
            "allow_insecure_http": True,
            "subject_claim": "oid",
            "access_rule": {"claim": "roles", "any_of": ["access"]},
            "role_mappings": [
                {"rule": {"claim": "roles", "any_of": ["admin"]}, "role": "proxy_admin"}
            ],
            "mcp_access_rules": {"managed_a": {"claim": "roles", "any_of": ["mcp-a"]}},
        }
        api = {
            "resource": BASE,
            "audience": "litellm-api",
            "scope_claim": "scp",
            "token_types": ["JWT"],
            "scopes": [{"name": "api://fixture/access", "token_value": "access"}],
        }
        response = httpx.put(
            BASE + "/oidc/settings",
            headers=admin,
            json={
                "expected_revision": current["revision"],
                "browser": browser,
                "api": api,
                "client_secret": "fixture-only",
            },
            trust_env=False,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_noop_save_conflicts_when_settings_change_during_discovery(self):
        import time
        from concurrent.futures import ThreadPoolExecutor

        current = self.enabled()
        httpx.post(
            "http://issuer:8080/control/metadata",
            json={"pause_next_discovery": True},
            trust_env=False,
        ).raise_for_status()
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(self.save, current)
            try:
                deadline = time.monotonic() + 5
                while not httpx.get(
                    "http://issuer:8080/control/discovery-state", trust_env=False
                ).json()["waiting"]:
                    self.assertLess(
                        time.monotonic(), deadline, "issuer did not receive discovery"
                    )
                    time.sleep(0.01)
                updated = copy.deepcopy(current["browser"])
                updated["session_ttl_seconds"] = 1800
                winning = self.save(current, browser=updated)
                self.assertEqual(winning.status_code, 200, winning.text)
            finally:
                httpx.post(
                    "http://issuer:8080/control/discovery-release",
                    json={},
                    trust_env=False,
                ).raise_for_status()
            stale = pending.result(timeout=10)
            self.assertEqual(stale.status_code, 409, stale.text)

    def test_native_key_cannot_execute_personal_mcp_tools(self):
        self.enabled()
        from test_mcp import PersonalMCPTests

        flow = PersonalMCPTests()
        flow.client = httpx.Client(base_url=BASE, trust_env=False)
        self.addCleanup(flow.client.close)
        provider = flow.headers(user="settings-mcp", roles=["access", "mcp-a"])
        code, verifier = flow.connect(provider, actor="settings-mcp-upstream")
        connected = flow.exchange(provider, code, verifier)
        self.assertEqual(connected.status_code, 200, connected.text)
        called = flow.client.post(
            "/mcp-rest/tools/call",
            headers=provider,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(called.status_code, 200, called.text)
        denied = flow.client.post(
            "/mcp-rest/tools/call",
            headers={"Authorization": "Bearer " + MASTER},
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertIn("provider user access token", denied.text)
        browser = self.browser_login(
            user="settings-mcp", roles=["access", "admin", "mcp-a"]
        )
        subject = browser.get("/oidc/session").json()["user_id"]
        key = flow.client.post(
            "/key/generate",
            headers={"Authorization": "Bearer " + MASTER},
            json={
                "user_id": subject,
                "models": ["fixture-model"],
                "object_permission": {"mcp_servers": ["managed_a"]},
            },
        )
        self.assertEqual(key.status_code, 200, key.text)
        native = {"Authorization": "Bearer " + key.json()["key"]}
        denied = flow.client.post(
            "/mcp-rest/tools/call",
            headers=native,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        httpx.delete(
            "http://mcp-upstream:8090/observed", trust_env=False
        ).raise_for_status()
        completion = flow.client.post(
            "/v1/chat/completions",
            headers=native,
            json={
                "model": "fixture-model",
                "messages": [
                    {"role": "user", "content": "Use the personal MCP connection"}
                ],
                "tools": [
                    {
                        "type": "mcp",
                        "server_url": "litellm_proxy/mcp/managed_a",
                        "server_label": "managed_a",
                        "require_approval": "never",
                    }
                ],
            },
        )
        self.assertEqual(completion.status_code, 200, completion.text)
        observed = httpx.get("http://mcp-upstream:8090/observed", trust_env=False)
        self.assertNotIn("mcp-fixture-access-", observed.text)

    def test_existing_mcp_session_observes_changed_server_policy(self):
        self.assert_mcp_policy_change(modern=False)

    def test_current_mcp_protocol_observes_changed_server_policy(self):
        self.assert_mcp_policy_change(modern=True)

    def assert_mcp_policy_change(self, *, modern):
        from test_api import ProviderAPITests
        from test_mcp import PersonalMCPTests

        current = self.enabled()
        flow = PersonalMCPTests()
        flow.client = httpx.Client(base_url=BASE, trust_env=False)
        self.addCleanup(flow.client.close)
        provider = flow.headers(
            user="settings-session-policy", roles=["access", "mcp-a"]
        )
        code, verifier = flow.connect(provider, actor="settings-session-upstream")
        connected = flow.exchange(provider, code, verifier)
        self.assertEqual(connected.status_code, 200, connected.text)
        session = (
            {
                **provider,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
            }
            if modern
            else ProviderAPITests.mcp_initialize(flow, provider)
        )

        if not modern:
            self.assertTrue(session["Mcp-Session-Id"])

        def request(method, params=None):
            params = dict(params or {})
            headers = dict(session)
            if modern:
                headers["MCP-Method"] = method
                if "name" in params:
                    headers["MCP-Name"] = params["name"]
                params["_meta"] = {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            return ProviderAPITests.mcp_request(flow, headers, method, params)

        listed, listing = request("tools/list")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertIn(
            "managed_a-whoami", [tool["name"] for tool in listing["result"]["tools"]]
        )
        params = {"name": "managed_a-whoami", "arguments": {}}
        called, result = request("tools/call", params)
        self.assertEqual(called.status_code, 200, called.text)
        self.assertIn("settings-session-upstream", str(result))
        browser = copy.deepcopy(current["browser"])
        browser["mcp_access_rules"]["managed_a"] = {
            "claim": "roles",
            "any_of": ["denied"],
        }
        changed = self.save(current, browser=browser)
        self.assertEqual(changed.status_code, 200, changed.text)
        httpx.delete(
            "http://mcp-upstream:8090/observed", trust_env=False
        ).raise_for_status()
        admitted = flow.client.get("/v1/models", headers=provider)
        self.assertEqual(admitted.status_code, 200, admitted.text)
        denied, result = request("tools/call", params)
        self.assertEqual(denied.status_code, 200, denied.text)
        self.assertTrue(result["result"].get("isError"), denied.text)
        self.assertIn("not allowed", str(result["result"]["content"]))
        observed = httpx.get("http://mcp-upstream:8090/observed", trust_env=False)
        self.assertNotIn("mcp-fixture-access-", observed.text)
        listed, listing = request("tools/list")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn(
            "managed_a-whoami", [tool["name"] for tool in listing["result"]["tools"]]
        )

    def browser_login(self, user="settings-admin", roles=None):
        httpx.post(
            "http://issuer:8080/control/actor",
            json={
                "sub": user,
                "oid": user,
                "email": user + "@example.test",
                "roles": roles or ["access", "admin"],
            },
            trust_env=False,
        ).raise_for_status()
        client = httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False)
        self.addCleanup(client.close)
        start = client.get("/oidc/login")
        self.assertEqual(start.status_code, 302, start.text)
        authorize = client.get(start.headers["location"])
        callback = client.get(authorize.headers["location"])
        self.assertEqual(callback.status_code, 303, callback.text)
        client.headers.update(
            {"Authorization": "Bearer oidc-browser-session", "Origin": BASE}
        )
        return client

    def save(self, current, **updates):
        payload = {
            "expected_revision": current["revision"],
            "browser": current["browser"],
            "api": current["api"],
            **updates,
        }
        return httpx.put(
            BASE + "/oidc/settings",
            headers={"Authorization": "Bearer " + MASTER},
            json=payload,
            trust_env=False,
        )

    def test_discovery_is_non_mutating_and_never_checks_a_credential(self):
        admin = {"Authorization": "Bearer " + MASTER}
        before = httpx.get(
            BASE + "/oidc/settings", headers=admin, trust_env=False
        ).json()
        result = httpx.post(
            BASE + "/oidc/settings/check",
            headers=admin,
            json={"issuer": "http://issuer:8080", "allow_insecure_http": True},
            trust_env=False,
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["credential_verified"])
        self.assertEqual(result.json()["code_challenge_methods_supported"], ["S256"])
        after = httpx.get(
            BASE + "/oidc/settings", headers=admin, trust_env=False
        ).json()
        self.assertEqual(before, after)

    def test_discovery_rejects_incompatible_pkce_and_malformed_metadata(self):
        admin = {"Authorization": "Bearer " + MASTER}
        payload = {"issuer": "http://issuer:8080", "allow_insecure_http": True}
        try:
            for invalid in (
                {"code_challenge_methods_supported": ["plain"]},
                {"scopes_supported": "invalid-scopes-metadata"},
            ):
                httpx.post(
                    "http://issuer:8080/control/metadata",
                    json={"metadata": invalid},
                    trust_env=False,
                ).raise_for_status()
                response = httpx.post(
                    BASE + "/oidc/settings/check",
                    headers=admin,
                    json=payload,
                    trust_env=False,
                )
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertNotIn("invalid-scopes-metadata", response.text)
            httpx.post(
                "http://issuer:8080/control/metadata",
                json={
                    "metadata": {},
                    "omit_metadata": ["code_challenge_methods_supported"],
                },
                trust_env=False,
            ).raise_for_status()
            unknown = httpx.post(
                BASE + "/oidc/settings/check",
                headers=admin,
                json=payload,
                trust_env=False,
            )
            self.assertEqual(unknown.status_code, 200, unknown.text)
            self.assertIsNone(unknown.json()["code_challenge_methods_supported"])
        finally:
            httpx.post(
                "http://issuer:8080/control/metadata",
                json={"metadata": {}, "omit_metadata": []},
                trust_env=False,
            ).raise_for_status()

    def test_invalid_save_preserves_active_configuration_and_redacts_input(self):
        current = self.enabled()
        browser = copy.deepcopy(current["browser"])
        browser["client_id"] = ""
        response = self.save(
            current, browser=browser, client_secret="secret-invalid-save-sentinel"
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("secret-invalid-save-sentinel", response.text)
        self.assertNotIn('"input"', response.text)
        self.assertEqual(
            httpx.get(
                BASE + "/oidc/settings",
                headers={"Authorization": "Bearer " + MASTER},
                trust_env=False,
            ).json(),
            {k: v for k, v in current.items() if k != "sessions_invalidated"},
        )

    def test_conflicts_noop_and_disable_do_not_revive_old_browser_sessions(self):
        current = self.enabled()
        browser = self.browser_login()
        unchanged = self.save(current)
        self.assertEqual(unchanged.status_code, 200, unchanged.text)
        self.assertEqual(unchanged.json()["revision"], current["revision"])
        self.assertFalse(unchanged.json()["sessions_invalidated"])
        self.assertEqual(browser.get("/oidc/session").status_code, 200)
        changed_config = copy.deepcopy(current["browser"])
        changed_config["session_ttl_seconds"] = 7200
        changed = self.save(current, browser=changed_config)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertTrue(changed.json()["sessions_invalidated"])
        self.assertEqual(browser.get("/oidc/session").status_code, 401)
        stale = self.save(current)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.headers.get("cache-control"), "no-store")
        self.assertEqual(
            stale.json()["detail"]["current_revision"], changed.json()["revision"]
        )
        fresh = self.browser_login(user="settings-second")
        disabled = httpx.post(
            BASE + "/oidc/settings/disable",
            headers={"Authorization": "Bearer " + MASTER},
            json={"expected_revision": changed.json()["revision"]},
            trust_env=False,
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["enabled"])
        self.assertTrue(disabled.json()["client_secret"]["configured"])
        self.assertNotEqual(fresh.get("/oidc/session").status_code, 200)
        self.assertEqual(
            httpx.get(BASE + "/fallback/login", trust_env=False).status_code, 200
        )
        reenabled = self.save(disabled.json())
        self.assertEqual(reenabled.status_code, 200, reenabled.text)
        self.assertEqual(fresh.get("/oidc/session").status_code, 401)

    def test_settings_require_admin_browser_and_same_origin_not_provider_admin(self):
        current = self.enabled()
        token = httpx.post(
            "http://issuer:8080/control/access-token",
            json={"roles": ["access", "admin"]},
            trust_env=False,
        ).json()["access_token"]
        with httpx.Client(
            base_url=BASE, trust_env=False, headers={"Authorization": "Bearer " + token}
        ) as provider:
            for method, path, body in (
                ("GET", "/oidc/settings", None),
                (
                    "POST",
                    "/oidc/settings/check",
                    {"issuer": "http://issuer:8080", "allow_insecure_http": True},
                ),
                (
                    "POST",
                    "/oidc/settings/disable",
                    {"expected_revision": current["revision"]},
                ),
                (
                    "PUT",
                    "/oidc/settings",
                    {
                        "expected_revision": current["revision"],
                        "browser": current["browser"],
                        "api": current["api"],
                    },
                ),
            ):
                response = provider.request(method, path, json=body)
                self.assertEqual(response.status_code, 403, response.text)
        viewer = self.browser_login(user="settings-viewer", roles=["access"])
        self.assertEqual(viewer.get("/oidc/settings").status_code, 403)
        admin = self.browser_login()
        self.assertEqual(admin.get("/oidc/settings").status_code, 200)
        denied = admin.post(
            "/oidc/settings/disable",
            headers={"Origin": "https://other.example.test"},
            json={"expected_revision": current["revision"]},
        )
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_settings_secret_replacement_invalidates_sessions_and_omission_retains_it(
        self,
    ):
        current = self.enabled()
        browser = self.browser_login()
        replacement = self.save(
            current, client_secret="rotated-settings-secret-sentinel-ä"
        )
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertNotIn("rotated-settings-secret-sentinel-ä", replacement.text)
        self.assertEqual(browser.get("/oidc/session").status_code, 401)
        retained = self.save(replacement.json())
        self.assertEqual(retained.status_code, 200, retained.text)
        self.assertEqual(retained.json()["revision"], replacement.json()["revision"])
        restored = self.save(retained.json(), client_secret="fixture-only")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(browser.get("/oidc/session").status_code, 401)

    def test_scope_admission_and_mcp_changes_apply_to_later_requests_on_all_workers(
        self,
    ):
        from concurrent.futures import ThreadPoolExecutor

        current = self.enabled()
        token = httpx.post(
            "http://issuer:8080/control/access-token",
            json={"roles": ["access", "admin", "mcp-a"]},
            trust_env=False,
        ).json()["access_token"]
        headers = {"Authorization": "Bearer " + token, "Connection": "close"}
        before = httpx.get(BASE + "/v1/mcp/server", headers=headers, trust_env=False)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertIn("managed_a", {item["server_id"] for item in before.json()})
        api = copy.deepcopy(current["api"])
        api["scopes"] = [{"name": "api://fixture/updated", "token_value": "updated"}]
        browser = copy.deepcopy(current["browser"])
        browser["mcp_access_rules"]["managed_a"]["any_of"] = [
            "different-mcp-permission"
        ]
        updated = self.save(current, browser=browser, api=api)
        self.assertEqual(updated.status_code, 200, updated.text)

        def observed(_):
            denied = httpx.get(BASE + "/v1/models", headers=headers, trust_env=False)
            metadata = httpx.get(
                BASE + "/.well-known/oauth-protected-resource",
                headers={"Connection": "close"},
                trust_env=False,
            )
            return denied, metadata

        with ThreadPoolExecutor(max_workers=8) as workers:
            for denied, metadata in workers.map(observed, range(16)):
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(
                    metadata.json()["scopes_supported"], ["api://fixture/updated"]
                )
        fresh = httpx.post(
            "http://issuer:8080/control/access-token",
            json={"scp": "updated", "roles": ["access", "admin", "mcp-a"]},
            trust_env=False,
        ).json()["access_token"]
        headers = {"Authorization": "Bearer " + fresh}
        self.assertEqual(
            httpx.get(
                BASE + "/v1/models", headers=headers, trust_env=False
            ).status_code,
            200,
        )
        denied = httpx.get(
            BASE + "/v1/mcp/server/managed_a/oauth-user-credential/status",
            headers=headers,
            trust_env=False,
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        browser["access_rule"]["any_of"] = ["new-admission"]
        changed = self.save(updated.json(), browser=browser)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(
            httpx.get(
                BASE + "/v1/models", headers=headers, trust_env=False
            ).status_code,
            403,
        )

    def test_unreadable_credentials_fail_closed_but_native_administration_recovers(
        self,
    ):
        import asyncio
        from prisma import Json, Prisma

        current = self.enabled()
        token = httpx.post(
            "http://issuer:8080/control/access-token", json={}, trust_env=False
        ).json()["access_token"]

        async def damage_and_check():
            db = Prisma()
            await db.connect()
            original = await db.litellm_config.find_unique(
                where={"param_name": "independent_oidc_settings"}
            )
            try:
                damaged = dict(original.param_value)
                damaged["client_secret_ciphertext"] = "unreadable-ciphertext-sentinel"
                await db.litellm_config.update(
                    where={"param_name": "independent_oidc_settings"},
                    data={"param_value": Json(damaged)},
                )
                with httpx.Client(base_url=BASE, trust_env=False) as client:
                    self.assertEqual(client.get("/oidc/login").status_code, 503)
                    self.assertEqual(
                        client.get(
                            "/v1/models", headers={"Authorization": "Bearer " + token}
                        ).status_code,
                        503,
                    )
                    native = {"Authorization": "Bearer " + MASTER}
                    self.assertEqual(
                        client.get("/oidc/settings", headers=native).status_code, 200
                    )
                    self.assertEqual(
                        client.get("/user/list", headers=native).status_code, 200
                    )
                    self.assertEqual(client.get("/fallback/login").status_code, 200)
                    discovery = client.get("/.well-known/litellm-ui-config")
                    self.assertEqual(discovery.status_code, 200, discovery.text)
                    self.assertFalse(discovery.json()["sso_configured"])
                    personal = client.post(
                        "/mcp-rest/tools/call",
                        headers=native,
                        json={
                            "server_id": "managed_a",
                            "name": "whoami",
                            "arguments": {},
                        },
                    )
                    self.assertNotEqual(personal.status_code, 200)
                    disabled = client.post(
                        "/oidc/settings/disable",
                        headers=native,
                        json={"expected_revision": current["revision"]},
                    )
                    self.assertEqual(disabled.status_code, 200, disabled.text)
                    self.assertFalse(disabled.json()["enabled"])
                    self.assertNotIn("unreadable-ciphertext-sentinel", disabled.text)
            finally:
                await db.litellm_config.update(
                    where={"param_name": "independent_oidc_settings"},
                    data={"param_value": Json(original.param_value)},
                )
                await db.disconnect()

        asyncio.run(damage_and_check())

    def test_stored_legacy_saml_setup_remains_visible_without_environment_mutation(
        self,
    ):
        import asyncio
        from prisma import Json, Prisma

        async def verify():
            db = Prisma()
            await db.connect()
            try:
                await db.litellm_ssoconfig.upsert(
                    where={"id": "sso_config"},
                    data={
                        "create": {
                            "id": "sso_config",
                            "sso_settings": Json(
                                {
                                    "saml_idp_metadata_xml": "<EntityDescriptor entityID='https://fixture.invalid' />"
                                }
                            ),
                        },
                        "update": {
                            "sso_settings": Json(
                                {
                                    "saml_idp_metadata_xml": "<EntityDescriptor entityID='https://fixture.invalid' />"
                                }
                            )
                        },
                    },
                )
                for _ in range(8):
                    response = httpx.get(
                        BASE + "/oidc/settings",
                        headers={
                            "Authorization": "Bearer " + MASTER,
                            "Connection": "close",
                        },
                        trust_env=False,
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(response.json()["legacy_sso_configured"])
                    self.assertNotIn("EntityDescriptor", response.text)
            finally:
                await db.litellm_ssoconfig.delete(where={"id": "sso_config"})
                await db.disconnect()

        asyncio.run(verify())

    def test_generic_configuration_cannot_bypass_oidc_activation(self):
        current = self.enabled()
        admin = {"Authorization": "Bearer " + MASTER}
        for body in (
            {"general_settings": {"oidc_identity": None}},
            {"independent_oidc_settings": {"enabled": False}},
        ):
            denied = httpx.post(
                BASE + "/config/update", headers=admin, json=body, trust_env=False
            )
            self.assertEqual(denied.status_code, 400, denied.text)
        rejected = httpx.post(
            BASE + "/config/field/update",
            headers=admin,
            json={
                "config_type": "general_settings",
                "field_name": "independent_oidc_settings",
                "field_value": {},
            },
            trust_env=False,
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        exposed = httpx.get(
            BASE + "/config/list",
            headers=admin,
            params={"config_type": "general_settings"},
            trust_env=False,
        )
        self.assertEqual(exposed.status_code, 200, exposed.text)
        self.assertNotIn("fixture-only", exposed.text)
        self.assertNotIn("client_secret_ciphertext", exposed.text)
        actual = httpx.get(
            BASE + "/oidc/settings", headers=admin, trust_env=False
        ).json()
        self.assertEqual(actual["revision"], current["revision"])
        self.assertTrue(actual["enabled"])

    def test_z_saved_configuration_and_browser_session_survive_cold_restart(self):
        import json
        from pathlib import Path

        state = Path("/restart/oidc-session.json")
        if os.environ.get("OIDC_SETTINGS_RESTART_PHASE") == "read":
            saved = json.loads(state.read_text())
            with httpx.Client(
                base_url=BASE, trust_env=False, cookies=saved["cookies"]
            ) as client:
                session = client.get("/oidc/session")
                self.assertEqual(session.status_code, 200, session.text)
                self.assertEqual(session.json()["user_id"], saved["user_id"])
                current = client.get(
                    "/oidc/settings", headers={"Authorization": "Bearer " + MASTER}
                ).json()
                self.assertEqual(current["revision"], saved["revision"])
                self.assertEqual(current["browser"], saved["browser"])
        else:
            current = self.enabled()
            browser = self.browser_login(user="settings-restart")
            session = browser.get("/oidc/session")
            self.assertEqual(session.status_code, 200, session.text)
            state.write_text(
                json.dumps(
                    {
                        "revision": current["revision"],
                        "browser": current["browser"],
                        "cookies": dict(browser.cookies),
                        "user_id": session.json()["user_id"],
                    }
                )
            )


@unittest.skipIf(
    os.environ.get("OIDC_SETTINGS_MODE") == "true", "requires file-owned OIDC fixture"
)
class FileOwnershipTests(unittest.TestCase):
    def test_native_recovery_login_keeps_the_existing_fixture_password(self):
        password = (
            "fixture-settings-admin-only"
            if os.environ.get("OIDC_SETTINGS_FILE_NULL") == "true"
            else MASTER
        )
        response = httpx.post(
            BASE + "/v2/login",
            json={"username": "admin", "password": password},
            trust_env=False,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["token"])

    def test_file_configuration_is_read_only_and_secrets_stay_in_environment(self):
        admin = {"Authorization": "Bearer " + MASTER}
        response = httpx.get(BASE + "/oidc/settings", headers=admin, trust_env=False)
        self.assertEqual(response.status_code, 200, response.text)
        current = response.json()
        self.assertEqual(current["source"], "file")
        self.assertTrue(current["read_only"])
        self.assertEqual(current["revision"], 0)
        self.assertNotIn("fixture-only", response.text)
        self.assertNotIn("fixture-session-secret", response.text)
        if os.environ.get("OIDC_SETTINGS_FILE_NULL") == "true":
            self.assertFalse(current["enabled"])
            self.assertIsNone(current["browser"])
            self.assertEqual(
                httpx.get(BASE + "/oidc/login", trust_env=False).status_code, 404
            )
        else:
            self.assertTrue(current["enabled"])
            self.assertEqual(current["client_secret"]["source"], "environment")
            self.assertEqual(
                current["client_secret"]["environment_variable"],
                "OIDC_TEST_CLIENT_SECRET",
            )
            denied = httpx.put(
                BASE + "/oidc/settings",
                headers=admin,
                json={
                    "expected_revision": 0,
                    "browser": current["browser"],
                    "api": current["api"],
                    "client_secret": "replacement-sentinel",
                },
                trust_env=False,
            )
            self.assertEqual(denied.status_code, 409, denied.text)
            self.assertNotIn("replacement-sentinel", denied.text)
        disabled = httpx.post(
            BASE + "/oidc/settings/disable",
            headers=admin,
            json={"expected_revision": 0},
            trust_env=False,
        )
        self.assertEqual(disabled.status_code, 409, disabled.text)
        self.assertEqual(disabled.json()["detail"]["code"], "file_managed")
