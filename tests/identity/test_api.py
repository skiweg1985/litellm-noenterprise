"""Provider API authentication observed through the running public proxy."""

import json
import time
import unittest

import httpx

BASE = "http://proxy:4000"
ISSUER = "http://issuer:8080"
MASTER = {"Authorization": "Bearer sk-identity-test-only"}


class ProviderAPITests(unittest.TestCase):
    def setUp(self):
        self.client = httpx.Client(base_url=BASE, trust_env=False)
        self.addCleanup(self.client.close)

    def token(self, **claims):
        result = httpx.post(ISSUER + "/control/access-token", json=claims, trust_env=False)
        self.assertEqual(result.status_code, 200)
        return result.json()["access_token"]

    def test_provider_token_creates_an_account_without_browser_login(self):
        headers = {"Authorization": "Bearer " + self.token(oid="api-first-visit")}
        result = self.client.get("/models", headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        listed = self.client.get("/user/list", headers=MASTER)
        self.assertEqual(listed.status_code, 200, listed.text)
        users = listed.json()["users"]
        self.assertTrue(any(user.get("sso_user_id", "").startswith("oidc-") for user in users))

    def browser_login(self, oid, roles):
        browser = httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False)
        self.addCleanup(browser.close)
        httpx.post(
            ISSUER + "/control/actor",
            json={"sub": "browser-sub", "oid": oid, "roles": roles},
            trust_env=False,
        )
        start = browser.get("/oidc/login")
        authorization = browser.get(start.headers["location"])
        callback = browser.get(authorization.headers["location"])
        self.assertEqual(callback.status_code, 303, callback.text)
        return browser

    def test_browser_and_api_grants_are_independent_for_the_same_stable_user(self):
        browser = self.browser_login("shared-admin", ["access", "admin"])
        original = browser.get("/oidc/session").json()
        before = {row["user_id"] for row in self.client.get("/user/list", headers=MASTER).json()["users"]}
        limited = {"Authorization": "Bearer " + self.token(oid="shared-admin", sub="different-api-sub")}
        self.assertEqual(self.client.get("/models", headers=limited).status_code, 200)
        self.assertEqual(self.client.get("/user/list", headers=limited).status_code, 403)
        self.assertEqual(browser.get("/oidc/session").json(), original)
        # Missing permissions on one API grant are not revocation of another grant.
        for claims in ({"roles": []}, {"scp": "other"}):
            denied = self.client.get(
                "/models", headers={"Authorization": "Bearer " + self.token(oid="shared-admin", **claims)}
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(browser.get("/oidc/session").json(), original)
        rows = self.client.get("/user/list", headers=MASTER).json()["users"]
        self.assertEqual({row["user_id"] for row in rows}, before)
        saved = self.client.get("/user/info", headers=MASTER, params={"user_id": original["user_id"]}).json()
        self.assertEqual(saved["user_info"]["user_role"], "proxy_admin")

    def test_api_admin_does_not_elevate_browser_or_native_key_privileges(self):
        browser = self.browser_login("shared-viewer", ["access"])
        original = browser.get("/oidc/session").json()
        elevated = {"Authorization": "Bearer " + self.token(oid="shared-viewer", roles=["access", "admin"])}
        self.assertEqual(self.client.get("/user/list", headers=elevated).status_code, 200)
        self.assertEqual(browser.get("/oidc/session").json(), original)
        self.assertEqual(
            browser.get("/user/list", headers={"Authorization": "Bearer oidc-browser-session"}).status_code,
            403,
        )
        before = {row["user_id"] for row in self.client.get("/user/list", headers=MASTER).json()["users"]}
        fresh = {"Authorization": "Bearer " + self.token(oid="new-api-admin", roles=["access", "admin"])}
        self.assertEqual(self.client.get("/user/list", headers=fresh).status_code, 200)
        created = [
            row
            for row in self.client.get("/user/list", headers=MASTER).json()["users"]
            if row["user_id"] not in before
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["user_role"], "internal_user_viewer")
        key = self.client.post("/key/generate", headers=MASTER, json={"user_id": created[0]["user_id"]})
        self.assertEqual(key.status_code, 200, key.text)
        denied = self.client.get("/user/list", headers={"Authorization": "Bearer " + key.json()["key"]})
        self.assertIn(denied.status_code, (401, 403), denied.text)

    def test_invalid_or_wrong_purpose_tokens_cannot_admit_a_user(self):
        invalid = [
            {"iss": "https://wrong.example"},
            {"aud": "upstream-mcp"},
            {"aud": "litellm-test", "nonce": "id-token"},
            {"nonce": "id-token-with-api-audience"},
            {"_wrong_key": True},
            {"exp": int(time.time()) - 20},
            {"iat": int(time.time()) + 60},
            {"_omit": ["exp"]},
            {"_omit": ["sub"]},
            {"_typ": "id+jwt"},
            {"token_use": "id"},
            {"idtyp": "app"},
        ]
        for claims in invalid:
            with self.subTest(claims=claims):
                response = self.client.get(
                    "/models", headers={"Authorization": "Bearer " + self.token(**claims)}
                )
                self.assertEqual(response.status_code, 401, response.text)
        for claims in (
            {"roles": []},
            {"scp": "access-extra"},
            {"scp": ["access"]},
            {"oid": None},
            {"identity_kind": "service"},
            {"_omit": ["identity_kind"]},
        ):
            with self.subTest(claims=claims):
                response = self.client.get(
                    "/models", headers={"Authorization": "Bearer " + self.token(**claims)}
                )
                self.assertEqual(response.status_code, 403, response.text)

    def test_public_discovery_and_challenges_describe_the_provider_grant(self):
        for suffix, resource in (("", BASE), ("/mcp", BASE + "/mcp")):
            metadata = self.client.get(
                "/.well-known/oauth-protected-resource" + suffix, headers={"Host": "attacker.invalid"}
            )
            self.assertEqual(metadata.status_code, 200, metadata.text)
            self.assertEqual(metadata.json()["resource"], resource)
            self.assertEqual(metadata.json()["authorization_servers"], [ISSUER])
            self.assertEqual(metadata.json()["scopes_supported"], ["api://litellm-api/access"])
            self.assertNotIn("roles_supported", metadata.json())
            provider = httpx.get(
                metadata.json()["authorization_servers"][0] + "/.well-known/openid-configuration",
                trust_env=False,
            )
            self.assertEqual(provider.json()["issuer"], ISSUER)
        anonymous = self.client.post(
            "/mcp/", headers={"Accept": "application/json, text/event-stream"}, json={}
        )
        self.assertEqual(anonymous.status_code, 401, anonymous.text)
        self.assertIn(
            BASE + "/.well-known/oauth-protected-resource/mcp", anonymous.headers["www-authenticate"]
        )
        wrong = self.client.get(
            "/models", headers={"Authorization": "Bearer " + self.token(aud="upstream-mcp")}
        )
        self.assertEqual(wrong.status_code, 401, wrong.text)
        self.assertIn('error="invalid_token"', wrong.headers["www-authenticate"])
        insufficient = self.client.get(
            "/models", headers={"Authorization": "Bearer " + self.token(scp="unrelated")}
        )
        self.assertEqual(insufficient.status_code, 403, insufficient.text)
        self.assertIn('scope="api://litellm-api/access"', insufficient.headers["www-authenticate"])
        self.assertIn('error="insufficient_scope"', insufficient.headers["www-authenticate"])

    def mcp_initialize(self, headers, path="/mcp/"):
        headers = {**headers, "Accept": "application/json, text/event-stream"}
        response = self.client.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "api-test", "version": "1"},
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        headers.update(
            {"Mcp-Session-Id": response.headers["mcp-session-id"], "MCP-Protocol-Version": "2025-06-18"}
        )
        response = self.client.post(
            path, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertIn(response.status_code, (200, 202), response.text)
        return headers

    def mcp_request(self, headers, method, params=None, path="/mcp/"):
        response = self.client.post(
            path, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}
        )
        if response.status_code != 200:
            return response, None
        if response.headers["content-type"].startswith("text/event-stream"):
            body = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
            return response, json.loads(body)
        return response, response.json()

    def test_api_token_mcp_sessions_cannot_be_reused_by_another_user(self):
        alice = {"Authorization": "Bearer " + self.token(oid="mcp-alice", roles=["access", "admin"])}
        session = self.mcp_initialize(alice)
        bob = {**session, "Authorization": "Bearer " + self.token(oid="mcp-bob", roles=["access", "admin"])}
        response, _ = self.mcp_request(bob, "tools/list")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"], {"code": -32001, "message": "Session not found"})
        # Rejection must not delete or transfer the original owner's session.
        response, _ = self.mcp_request(session, "tools/list")
        self.assertEqual(response.status_code, 200, response.text)
        fresh = self.mcp_initialize({"Authorization": bob["Authorization"]})
        response, _ = self.mcp_request(fresh, "tools/list")
        self.assertEqual(response.status_code, 200, response.text)

    def test_personal_routes_require_user_identity_and_native_workflows_still_work(self):
        for headers in (MASTER, {"Authorization": "Bearer " + self.token()}):
            completion = self.client.post(
                "/chat/completions",
                headers=headers,
                json={"model": "fixture-model", "messages": [{"role": "user", "content": "test"}]},
            )
            self.assertEqual(completion.status_code, 200, completion.text)
            self.assertEqual(completion.json()["choices"][0]["message"]["content"], "fixture completion")
        self.assertEqual(self.client.get("/v1/mcp/server", headers=MASTER).status_code, 200)
        for path in ("/v1/mcp/user-credentials", "/v1/mcp/server/unknown/oauth-user-credential/status"):
            response = self.client.get(path, headers=MASTER)
            self.assertEqual(response.status_code, 403, response.text)
        response = self.client.post(
            "/v1/mcp/server/unknown/oauth-user-credential",
            headers=MASTER,
            json={"access_token": "fixture-upstream"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        listing = self.client.get(
            "/v1/mcp/user-credentials", headers={"Authorization": "Bearer " + self.token()}
        )
        self.assertEqual(listing.status_code, 200, listing.text)

    def test_delegate_receives_only_the_separate_upstream_token(self):
        provider = self.token(oid="delegate-user", roles=["access", "admin"])
        for incoming in (
            {
                "Authorization": "Bearer " + provider,
                "x-mcp-delegate-authorization": "Bearer fixture-upstream",
            },
            {"x-litellm-api-key": provider, "Authorization": "Bearer fixture-upstream"},
        ):
            httpx.delete("http://mcp-upstream:8090/observed", trust_env=False)
            session = self.mcp_initialize(incoming, "/mcp/delegate")
            response, body = self.mcp_request(session, "tools/list", path="/mcp/delegate")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("result", body, body)
            self.assertTrue(body["result"]["tools"], body)
            seen = httpx.get("http://mcp-upstream:8090/observed", trust_env=False).json()
            self.assertTrue(seen)
            self.assertNotIn(provider, json.dumps(seen))
            self.assertTrue(any(row.get("authorization") == "Bearer fixture-upstream" for row in seen), seen)

    def test_native_key_cannot_use_an_existing_personal_oauth_connection(self):
        browser = self.browser_login("personal-owner", ["access", "admin"])
        user_id = browser.get("/oidc/session").json()["user_id"]
        provider = {"Authorization": "Bearer " + self.token(oid="personal-owner", roles=["access", "admin"])}
        servers = self.client.get("/v1/mcp/server", headers=MASTER).json()
        self.assertIsInstance(servers, list, servers)
        server_id = next(server["server_id"] for server in servers if server.get("server_name") == "personal")
        saved = self.client.post(
            "/v1/mcp/server/" + server_id + "/oauth-user-credential",
            headers=provider,
            json={"access_token": "fixture-personal", "expires_in": 3600},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        session = self.mcp_initialize(provider, "/mcp/personal")
        response, body = self.mcp_request(session, "tools/list", path="/mcp/personal")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(body.get("result", {}).get("tools"), body)
        key = self.client.post(
            "/key/generate",
            headers=MASTER,
            json={"user_id": user_id, "metadata": {"oidc_identity_source": "provider"}},
        )
        self.assertEqual(key.status_code, 200, key.text)
        native = {"Authorization": "Bearer " + key.json()["key"]}
        # Native access still initializes; resolution must not open the user's vault.
        session = self.mcp_initialize(native, "/mcp/personal")
        httpx.delete("http://mcp-upstream:8090/observed", trust_env=False)
        response, body = self.mcp_request(session, "tools/list", path="/mcp/personal")
        seen = httpx.get("http://mcp-upstream:8090/observed", trust_env=False).json()
        self.assertNotIn("fixture-personal", json.dumps(seen))
        self.assertFalse(body and body.get("result", {}).get("tools"), body)

    def test_native_custom_key_with_dots_is_not_misclassified_as_a_provider_token(self):
        generated = self.client.post(
            "/key/generate",
            headers=MASTER,
            json={"key": "sk-native.key.with-two-dots-acceptance", "models": ["fixture-model"]},
        )
        self.assertEqual(generated.status_code, 200, generated.text)
        result = self.client.get("/models", headers={"Authorization": "Bearer " + generated.json()["key"]})
        self.assertEqual(result.status_code, 200, result.text)

    def test_parameterized_inference_routes_accept_a_user_access_token(self):
        response = self.client.post(
            "/openai/deployments/fixture-model/chat/completions",
            headers={"Authorization": "Bearer " + self.token()},
            json={"messages": [{"role": "user", "content": "test"}]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "fixture completion")


if __name__ == "__main__":
    unittest.main()
