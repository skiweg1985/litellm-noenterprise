"""Personal MCP permissions and OAuth lifecycle through the public gateway."""

import unittest
import base64
import hashlib
from urllib.parse import urlparse, parse_qs

import httpx

BASE = "http://proxy:4000"
ISSUER = "http://issuer:8080"


class PersonalMCPTests(unittest.TestCase):
    def setUp(self):
        self.client = httpx.Client(base_url=BASE, trust_env=False)
        self.addCleanup(self.client.close)

    def headers(self, user="mcp-alice", roles=None):
        response = httpx.post(
            ISSUER + "/control/access-token",
            json={
                "oid": user,
                "roles": roles if roles is not None else ["access", "mcp-a"],
            },
            trust_env=False,
        )
        response.raise_for_status()
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def test_claim_rules_grant_visibility_and_deny_direct_connect_even_for_admin(self):
        headers = self.headers()
        response = self.client.get("/v1/mcp/server", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        ids = {row["server_id"] for row in response.json()}
        self.assertIn("managed_a", ids)
        self.assertNotIn("managed_b", ids)
        for roles in (["access"], ["access", "admin"]):
            denied = self.client.get(
                "/v1/mcp/server/oauth/managed_b/authorize",
                params={
                    "redirect_uri": BASE + "/ui/mcp/oauth/callback",
                    "client_id": "mcp-test",
                },
                headers=self.headers(roles=roles),
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            status = self.client.get(
                "/v1/mcp/server/managed_b/oauth-user-credential/status",
                headers=self.headers(roles=roles),
            )
            self.assertEqual(status.status_code, 403, status.text)

    def connect(
        self, headers, server="managed_a", actor="alice", expires_in=3600, refresh=True
    ):
        httpx.post(
            "http://mcp-upstream:8090/control/oauth",
            json={
                "actor": actor,
                "expires_in": expires_in,
                "refresh": refresh,
                "fail_refresh": False,
                "pause_refresh": False,
                "refresh_unavailable": False,
            },
            trust_env=False,
        ).raise_for_status()
        verifier = "fixture-pkce-verifier-that-is-at-least-43-characters-long"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        start = self.client.get(
            "/v1/mcp/server/oauth/" + server + "/authorize",
            headers=headers,
            params={
                "redirect_uri": BASE + "/ui/mcp/oauth/callback",
                "state": "fixture-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "client_id": "mcp-test",
            },
        )
        self.assertIn(start.status_code, (302, 307), start.text)
        consent = self.client.get(start.headers["location"])
        callback = self.client.get(consent.headers["location"])
        self.assertIn(callback.status_code, (302, 307), callback.text)
        params = parse_qs(urlparse(callback.headers["location"]).query)
        self.assertEqual(params["state"], ["fixture-state"])
        return params["code"][0], verifier

    def exchange(self, headers, code, verifier, server="managed_a"):
        return self.client.post(
            "/v1/mcp/server/oauth/" + server + "/token",
            headers=headers,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": "mcp-test",
                "redirect_uri": BASE + "/ui/mcp/oauth/callback",
            },
        )

    def test_managed_oauth_is_user_bound_and_returns_only_connection_status(self):
        alice = self.headers()
        code, verifier = self.connect(alice)
        wrong = self.exchange(self.headers(user="mcp-bob"), code, verifier)
        self.assertEqual(wrong.status_code, 403, wrong.text)
        wrong_verifier = self.exchange(alice, code, "wrong-verifier")
        self.assertEqual(wrong_verifier.status_code, 403, wrong_verifier.text)
        wrong_server = self.exchange(
            self.headers(roles=["access", "mcp-a", "mcp-b"]),
            code,
            verifier,
            "managed_b",
        )
        self.assertNotEqual(wrong_server.status_code, 200, wrong_server.text)
        exchanged = self.exchange(alice, code, verifier)
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        self.assertTrue(exchanged.json()["has_credential"])
        self.assertEqual(exchanged.json()["connection_status"], "connected")
        self.assertNotIn("access_token", exchanged.json())
        self.assertNotIn("refresh_token", exchanged.json())
        replay = self.exchange(alice, code, verifier)
        self.assertNotEqual(replay.status_code, 200, replay.text)
        status = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status", headers=alice
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["has_credential"])
        bob = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status",
            headers=self.headers(user="mcp-bob"),
        )
        self.assertFalse(bob.json()["has_credential"])

    def test_managed_tool_use_is_isolated_and_requires_provider_identity(self):
        from test_api import ProviderAPITests

        alice = self.headers(user="tool-alice")
        code, verifier = self.connect(alice, actor="alice-upstream")
        self.assertEqual(self.exchange(alice, code, verifier).status_code, 200)
        response = self.client.post(
            "/mcp-rest/tools/call",
            headers=alice,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("alice-upstream", response.text)
        bob = self.client.post(
            "/mcp-rest/tools/call",
            headers=self.headers(user="tool-bob"),
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertNotIn("alice-upstream", bob.text)
        self.assertNotEqual(bob.status_code, 200, bob.text)
        denied = self.client.post(
            "/mcp-rest/tools/call",
            headers=self.headers(user="tool-alice", roles=["access"]),
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        # The same browser identity can manage its connection, but cannot execute a personal tool.
        browser = ProviderAPITests.browser_login(
            self, "tool-alice", ["access", "mcp-a"]
        )
        called = browser.post(
            "/mcp-rest/tools/call",
            headers={"Authorization": "Bearer oidc-browser-session", "Origin": BASE},
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(called.status_code, 403, called.text)
        session = ProviderAPITests.mcp_initialize(self, alice, "/mcp/managed_a")
        response, body = ProviderAPITests.mcp_request(
            self, session, "tools/list", path="/mcp/managed_a"
        )
        self.assertEqual(response.status_code, 200, response.text)
        names = [tool["name"] for tool in body.get("result", {}).get("tools", [])]
        self.assertTrue(any(name.endswith("whoami") for name in names), body)

    def test_managed_refresh_logout_retention_and_disconnect(self):
        from test_api import ProviderAPITests

        alice = self.headers(user="lifecycle-alice")
        code, verifier = self.connect(alice, actor="lifecycle-upstream", expires_in=0)
        self.assertEqual(self.exchange(alice, code, verifier).status_code, 200)
        browser = ProviderAPITests.browser_login(
            self, "lifecycle-alice", ["access", "mcp-a"]
        )
        self.assertEqual(
            browser.post("/oidc/logout", headers={"Origin": BASE}).status_code, 200
        )
        status = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status", headers=alice
        )
        self.assertTrue(status.json()["has_credential"])
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=alice,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(called.status_code, 200, called.text)
        self.assertIn("lifecycle-upstream", called.text)
        events = httpx.get(
            "http://mcp-upstream:8090/oauth/events", trust_env=False
        ).json()
        self.assertTrue(
            any(
                row == {"event": "refresh", "actor": "lifecycle-upstream"}
                for row in events
            ),
            events,
        )
        hidden = self.client.get(
            "/v1/mcp/user-credentials",
            headers=self.headers(user="lifecycle-alice", roles=["access"]),
        )
        self.assertNotIn("managed_a", hidden.text)
        disconnected = self.client.delete(
            "/v1/mcp/server/managed_a/oauth-user-credential", headers=alice
        )
        self.assertEqual(disconnected.status_code, 200, disconnected.text)
        self.assertFalse(disconnected.json()["has_credential"])
        self.assertEqual(disconnected.json()["upstream_revocation"], "revoked")
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=alice,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertNotEqual(called.status_code, 200, called.text)
        self.assertNotIn("lifecycle-upstream", called.text)

    def test_delegate_never_creates_managed_credentials(self):
        from test_api import ProviderAPITests

        headers = self.headers(user="delegate-personal", roles=["access", "admin"])
        servers = self.client.get("/v1/mcp/server", headers=headers).json()
        sid = next(
            row["server_id"] for row in servers if row["server_name"] == "delegate"
        )
        saved = self.client.post(
            "/v1/mcp/server/" + sid + "/oauth-user-credential",
            headers=headers,
            json={"access_token": "client-supplied-sentinel"},
        )
        self.assertEqual(saved.status_code, 400, saved.text)
        session = ProviderAPITests.mcp_initialize(
            self,
            {
                **headers,
                "x-mcp-delegate-authorization": "Bearer client-supplied-sentinel",
            },
            "/mcp/delegate",
        )
        response, body = ProviderAPITests.mcp_request(
            self, session, "tools/list", path="/mcp/delegate"
        )
        self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get(
            "/v1/mcp/server/" + sid + "/oauth-user-credential/status", headers=headers
        )
        self.assertFalse(status.json()["has_credential"])
        self.assertEqual(status.json()["connection_mode"], "client_supplied")

    def test_failed_refresh_requires_reconnect_and_reconnect_restores_access(self):
        headers = self.headers(user="failed-refresh")
        code, verifier = self.connect(headers, actor="refresh-failure", expires_in=0)
        self.assertEqual(self.exchange(headers, code, verifier).status_code, 200)
        httpx.post(
            "http://mcp-upstream:8090/control/oauth",
            json={"fail_refresh": True},
            trust_env=False,
        )
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertNotEqual(called.status_code, 200, called.text)
        status = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status", headers=headers
        )
        self.assertEqual(status.json()["connection_status"], "reconnect_required")
        code, verifier = self.connect(headers, actor="refresh-restored")
        self.assertEqual(self.exchange(headers, code, verifier).status_code, 200)
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(called.status_code, 200, called.text)
        self.assertIn("refresh-restored", called.text)

    def test_disconnect_cannot_be_undone_by_inflight_refresh(self):
        from concurrent.futures import ThreadPoolExecutor

        headers = self.headers(user="refresh-race")
        code, verifier = self.connect(headers, actor="race-actor", expires_in=0)
        self.assertEqual(self.exchange(headers, code, verifier).status_code, 200)
        httpx.post(
            "http://mcp-upstream:8090/control/oauth",
            json={"pause_refresh": True},
            trust_env=False,
        )
        with ThreadPoolExecutor() as pool:
            future = pool.submit(
                httpx.post,
                BASE + "/mcp-rest/tools/call",
                headers=headers,
                json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
                trust_env=False,
                timeout=40,
            )
            httpx.get(
                "http://mcp-upstream:8090/control/refresh-wait",
                trust_env=False,
                timeout=25,
            ).raise_for_status()
            deleted = self.client.delete(
                "/v1/mcp/server/managed_a/oauth-user-credential", headers=headers
            )
            self.assertEqual(deleted.status_code, 200, deleted.text)
            httpx.post(
                "http://mcp-upstream:8090/control/refresh-release", trust_env=False
            ).raise_for_status()
            result = future.result(timeout=30)
            self.assertNotEqual(result.status_code, 200, result.text)
        status = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status", headers=headers
        )
        self.assertFalse(status.json()["has_credential"])

    def test_alternate_token_routes_cannot_bypass_managed_user_binding(self):
        alice = self.headers(user="alternate-alice")
        code, verifier = self.connect(alice)
        for path in ("/managed_a/token", "/token"):
            for headers in ({}, self.headers(user="alternate-bob")):
                response = self.client.post(
                    path,
                    headers=headers,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": verifier,
                        "client_id": "mcp-test",
                        "resource": BASE + "/mcp/managed_a",
                        "redirect_uri": BASE + "/ui/mcp/oauth/callback",
                    },
                )
                self.assertNotEqual(response.status_code, 200, response.text)
                self.assertNotIn("mcp-fixture-access-", response.text)
        response = self.exchange(alice, code, verifier)
        self.assertEqual(response.status_code, 200, response.text)

    def test_connection_survives_cold_restart(self):
        import os

        headers = self.headers(user="persistent-alice")
        if os.environ.get("MCP_RESTART_PHASE") != "read":
            code, verifier = self.connect(headers, actor="persistent-upstream")
            self.assertEqual(self.exchange(headers, code, verifier).status_code, 200)
        status = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status", headers=headers
        )
        self.assertTrue(status.json()["has_credential"])
        response = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("persistent-upstream", response.text)

    def test_native_user_ceiling_and_virtual_tools_still_apply_to_claim_grants(self):
        from test_api import ProviderAPITests, MASTER

        browser = ProviderAPITests.browser_login(
            self, "ceiling-user", ["access", "mcp-a", "mcp-b"]
        )
        user_id = browser.get("/oidc/session").json()["user_id"]
        updated = self.client.post(
            "/user/update",
            headers=MASTER,
            json={
                "user_id": user_id,
                "object_permission": {
                    "mcp_servers": ["managed_b"],
                    "mcp_tool_search_enabled": True,
                },
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        headers = self.headers(user="ceiling-user", roles=["access", "mcp-a", "mcp-b"])
        visible = self.client.get("/v1/mcp/server", headers=headers)
        self.assertEqual({row["server_id"] for row in visible.json()}, {"managed_b"})
        admin = self.headers(
            user="ceiling-user", roles=["access", "admin", "mcp-a", "mcp-b"]
        )
        denied_admin = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status", headers=admin
        )
        self.assertEqual(denied_admin.status_code, 403, denied_admin.text)
        code, verifier = self.connect(
            headers, server="managed_b", actor="ceiling-actor"
        )
        self.assertEqual(
            self.exchange(headers, code, verifier, server="managed_b").status_code, 200
        )
        search = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={
                "name": "mcp_tool_search",
                "arguments": {"query": "whoami", "top_k": 10},
            },
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertIn("managed_b", search.text)
        self.assertNotIn("managed_a", search.text)
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={
                "name": "mcp_tool_call",
                "arguments": {"tool_name": "managed_b-whoami", "arguments": {}},
            },
        )
        self.assertEqual(called.status_code, 200, called.text)
        self.assertIn("ceiling-actor", called.text)
        denied = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={
                "name": "mcp_tool_call",
                "arguments": {"tool_name": "managed_a-whoami", "arguments": {}},
            },
        )
        self.assertNotIn("ceiling-actor", denied.text)
        self.assertTrue(
            denied.status_code != 200 or denied.json().get("isError"), denied.text
        )

    def test_browser_session_cannot_execute_personal_tools_over_mcp(self):
        from test_api import ProviderAPITests

        headers = self.headers(user="browser-tool-user")
        code, verifier = self.connect(headers, actor="browser-upstream")
        self.assertEqual(self.exchange(headers, code, verifier).status_code, 200)
        browser = ProviderAPITests.browser_login(
            self, "browser-tool-user", ["access", "mcp-a"]
        )
        old = self.client
        self.client = browser
        try:
            session = ProviderAPITests.mcp_initialize(
                self,
                {"Authorization": "Bearer oidc-browser-session", "Origin": BASE},
                "/mcp/managed_a",
            )
            response, body = ProviderAPITests.mcp_request(
                self, session, "tools/list", path="/mcp/managed_a"
            )
            self.assertFalse(body and body.get("result", {}).get("tools"), body)
        finally:
            self.client = old

    def test_temporary_refresh_failure_recovers_without_reauthorization(self):
        headers = self.headers(user="transient-user")
        code, verifier = self.connect(headers, actor="transient-upstream", expires_in=0)
        self.assertEqual(self.exchange(headers, code, verifier).status_code, 200)
        httpx.post(
            "http://mcp-upstream:8090/control/oauth",
            json={"refresh_unavailable": True},
            trust_env=False,
        )
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertNotEqual(called.status_code, 200, called.text)
        httpx.post(
            "http://mcp-upstream:8090/control/oauth",
            json={"refresh_unavailable": False},
            trust_env=False,
        )
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(called.status_code, 200, called.text)
        self.assertIn("transient-upstream", called.text)

    def test_upstream_revocation_reports_reconnect_without_using_another_connection(
        self,
    ):
        headers = self.headers(user="revoked-user")
        code, verifier = self.connect(headers, actor="revoked-upstream")
        self.assertEqual(self.exchange(headers, code, verifier).status_code, 200)
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertIn("revoked-upstream", called.text)
        httpx.post(
            "http://mcp-upstream:8090/control/revoke",
            json={"actor": "revoked-upstream"},
            trust_env=False,
        )
        called = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertTrue(
            called.status_code != 200 or called.json().get("isError"), called.text
        )
        status = self.client.get(
            "/v1/mcp/server/managed_a/oauth-user-credential/status", headers=headers
        )
        self.assertEqual(status.json()["connection_status"], "reconnect_required")

    def test_browser_connect_is_reused_by_provider_api_for_same_stable_identity(self):
        from test_api import ProviderAPITests

        browser = ProviderAPITests.browser_login(
            self, "browser-connect", ["access", "mcp-a"]
        )
        old = self.client
        self.client = browser
        try:
            code, verifier = self.connect({}, actor="browser-connected-upstream")
            saved = self.exchange(
                {"Authorization": "Bearer oidc-browser-session", "Origin": BASE},
                code,
                verifier,
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertTrue(saved.json()["has_credential"])
            self.assertNotIn("mcp-fixture-", saved.text)
        finally:
            self.client = old
        headers = self.headers(user="browser-connect")
        response = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("browser-connected-upstream", response.text)
        session = ProviderAPITests.mcp_initialize(self, headers)
        response, body = ProviderAPITests.mcp_request(self, session, "tools/list")
        names = [tool["name"] for tool in body.get("result", {}).get("tools", [])]
        self.assertIn("managed_a-whoami", names)
        self.assertFalse(any(name.startswith("managed_b-") for name in names))
        response, body = ProviderAPITests.mcp_request(
            self, session, "tools/call", {"name": "managed_b-whoami", "arguments": {}}
        )
        self.assertTrue(
            response.status_code != 200
            or body.get("error")
            or body.get("result", {}).get("isError"),
            body,
        )

    def test_disconnect_reports_unsupported_revocation_and_is_idempotent(self):
        headers = self.headers(user="disconnect-unsupported", roles=["access", "admin"])
        servers = self.client.get("/v1/mcp/server", headers=headers).json()
        sid = next(
            row["server_id"] for row in servers if row["server_name"] == "personal"
        )
        saved = self.client.post(
            "/v1/mcp/server/" + sid + "/oauth-user-credential",
            headers=headers,
            json={"access_token": "fixture-unsupported-revoke", "expires_in": 3600},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        result = self.client.delete(
            "/v1/mcp/server/" + sid + "/oauth-user-credential", headers=headers
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["upstream_revocation"], "unsupported")
        repeated = self.client.delete(
            "/v1/mcp/server/" + sid + "/oauth-user-credential", headers=headers
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["upstream_revocation"], "no_credential")

    def test_managed_server_without_registered_client_reports_operator_setup(self):
        headers = self.headers(user="setup-user", roles=["access", "mcp-setup"])
        status = self.client.get(
            "/v1/mcp/server/managed_unconfigured/oauth-user-credential/status",
            headers=headers,
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["connection_status"], "unavailable")
        self.assertEqual(status.json()["configuration_error"], "oauth_client_required")
        result = self.client.get(
            "/v1/mcp/server/oauth/managed_unconfigured/authorize",
            headers=headers,
            params={
                "redirect_uri": BASE + "/ui/mcp/oauth/callback",
                "client_id": "caller-client",
            },
        )
        self.assertEqual(result.status_code, 400, result.text)
        self.assertIn("oauth_client_required", result.text)
