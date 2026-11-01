"""Public HTTP journeys against the real proxy and an owned OIDC issuer."""

import copy
import time
import unittest

import httpx

BASE = "http://proxy:4000"
ISSUER = "http://issuer:8080"


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.browser = httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False)
        self.addCleanup(self.browser.close)
        httpx.post(ISSUER + "/control/metadata", json={"method": "client_secret_post"}, trust_env=False)
        httpx.post(
            ISSUER + "/control/actor",
            json={"sub": "alice", "oid": "stable-alice", "email": "shared@example.test", "roles": ["access"]},
            trust_env=False,
        )

    def login(self):
        start = self.browser.get("/sso/key/generate")
        self.assertEqual(start.status_code, 302, start.text)
        authorization = self.browser.get(start.headers["location"])
        self.assertEqual(authorization.status_code, 302, authorization.text)
        return self.browser.get(authorization.headers["location"])

    def test_eligible_user_gets_minimal_personal_session(self):
        callback = self.login()
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertEqual(callback.headers["location"], "/ui/connect/")
        session = self.browser.get("/oidc/session")
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(session.json()["role"], "internal_user_viewer")
        self.assertFalse(session.json()["admin_access"])
        self.assertNotIn("opaque-provider-token", str(self.browser.cookies))
        denied = self.browser.get("/user/list", headers={"Authorization": "Bearer oidc-browser-session"})
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_native_logout_revokes_browser_session(self):
        self.assertEqual(self.login().status_code, 303)
        response = self.browser.post(
            "/session/logout", headers={"Authorization": "Bearer oidc-browser-session", "Origin": BASE}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.browser.get("/oidc/session").status_code, 401)

    def test_logout_does_not_revoke_another_browser(self):
        self.assertEqual(self.login().status_code, 303)
        other = httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False)
        self.addCleanup(other.close)
        original = self.browser
        self.browser = other
        self.assertEqual(self.login().status_code, 303)
        self.browser = original
        self.assertEqual(
            original.post(
                "/session/logout", headers={"Authorization": "Bearer oidc-browser-session", "Origin": BASE}
            ).status_code,
            200,
        )
        session = other.get("/oidc/session")
        self.assertEqual(session.status_code, 200, session.text)

    def test_provider_basic_client_auth_is_supported(self):
        httpx.post(ISSUER + "/control/metadata", json={"method": "client_secret_basic"}, trust_env=False)
        callback = self.login()
        self.assertEqual(callback.status_code, 303, callback.text)

    def actor(self, **overrides):
        claims = {"sub": "alice", "oid": "stable-alice", "email": "shared@example.test", "roles": ["access"]}
        claims.update(overrides)
        httpx.post(ISSUER + "/control/actor", json=claims, trust_env=False)

    def test_role_priority_and_relogin_demotion_are_persisted(self):
        self.actor(roles=["access", "operator", "admin"])
        self.assertEqual(self.login().headers["location"], "/ui/")
        first = self.browser.get("/oidc/session").json()
        self.assertEqual(first["role"], "proxy_admin")
        old_cookies = dict(self.browser.cookies)
        self.actor(roles=["access", "operator"])
        self.assertEqual(self.login().headers["location"], "/ui/connect/")
        second = self.browser.get("/oidc/session").json()
        self.assertEqual(second["user_id"], first["user_id"])
        self.assertEqual(second["role"], "internal_user")
        old = httpx.get(BASE + "/oidc/session", cookies=old_cookies, trust_env=False)
        self.assertEqual(old.status_code, 401, old.text)
        info = httpx.get(
            BASE + "/user/info",
            headers={"Authorization": "Bearer sk-identity-test-only"},
            params={"user_id": first["user_id"]},
            trust_env=False,
        )
        self.assertEqual(info.json()["user_info"]["user_role"], "internal_user")
        self.actor(roles=["access"])
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.browser.get("/oidc/session").json()["role"], "internal_user_viewer")

    def test_stable_subject_is_used_and_email_does_not_link_accounts(self):
        self.assertEqual(self.login().status_code, 303)
        first = self.browser.get("/oidc/session").json()["user_id"]
        self.actor(sub="different-client-subject")
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.browser.get("/oidc/session").json()["user_id"], first)
        self.actor(sub="different-user", oid="stable-other")
        self.assertEqual(self.login().status_code, 303)
        self.assertNotEqual(self.browser.get("/oidc/session").json()["user_id"], first)

    def test_missing_admission_rejects_new_and_existing_accounts(self):
        self.actor(oid="never-admitted", roles=[])
        self.assertEqual(self.login().status_code, 403)
        self.actor()
        self.assertEqual(self.login().status_code, 303)
        self.actor(roles=[])
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(self.browser.get("/oidc/session").status_code, 401)

    def test_invalid_identity_tokens_are_rejected(self):
        for override in (
            {"iss": "http://wrong-issuer"},
            {"aud": "another-app"},
            {"nonce": "wrong"},
            {"exp": int(time.time()) - 60},
            {"_sign_with_wrong_key": True},
            {"azp": "other-app"},
        ):
            with self.subTest(override=override):
                self.actor(**override)
                callback = self.login()
                self.assertEqual(callback.status_code, 401, callback.text)
        self.actor(oid=None)
        self.assertEqual(self.login().status_code, 403)

    def test_callback_is_bound_to_starting_browser_and_single_use_code(self):
        start = self.browser.get("/oidc/login")
        authorization = self.browser.get(start.headers["location"])
        location = authorization.headers["location"]
        outsider = httpx.get(location, trust_env=False)
        self.assertEqual(outsider.status_code, 401)
        self.assertEqual(self.browser.get(location + "wrong-state").status_code, 401)
        callback = self.browser.get(location)
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertEqual(self.browser.get(location).status_code, 401)

    def test_marker_without_cookie_and_cross_origin_mutation_are_denied(self):
        self.assertEqual(self.login().status_code, 303)
        marker = {"Authorization": "Bearer oidc-browser-session"}
        self.assertEqual(httpx.get(BASE + "/user/list", headers=marker, trust_env=False).status_code, 401)
        for origin in (None, "https://another.example"):
            headers = marker if origin is None else {**marker, "Origin": origin}
            result = self.browser.post("/session/logout", headers=headers)
            self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(self.browser.get("/oidc/session").status_code, 200)
        cookies = dict(self.browser.cookies)
        cookies["litellm_oidc_session"] = "tampered"
        self.assertEqual(httpx.get(BASE + "/oidc/session", cookies=cookies, trust_env=False).status_code, 401)
        denied = self.browser.post("/v1/mcp/server", headers={**marker, "Origin": BASE}, json={})
        self.assertEqual(denied.status_code, 403, denied.text)

    def native_identity_key(self):
        user_id = self.browser.get("/oidc/session").json()["user_id"]
        admin_headers = {"Authorization": "Bearer sk-identity-test-only"}
        generated = httpx.post(
            BASE + "/key/generate",
            headers=admin_headers,
            json={"user_id": user_id, "allowed_routes": ["/user/update"]},
            trust_env=False,
        )
        self.assertEqual(generated.status_code, 200, generated.text)
        info = httpx.get(BASE + "/user/info", headers=admin_headers, params={"user_id": user_id}, trust_env=False)
        self.assertEqual(info.status_code, 200, info.text)
        return user_id, {"Authorization": "Bearer " + generated.json()["key"]}, info.json()["user_info"]["metadata"]

    def test_native_key_cannot_forge_browser_claims_in_user_metadata(self):
        httpx.post(
            ISSUER + "/control/actor",
            json={"sub": "alice", "oid": "stable-alice", "roles": ["access", "operator"]},
            trust_env=False,
        )
        self.assertEqual(self.login().status_code, 303)
        user_id, headers, metadata = self.native_identity_key()
        forged = copy.deepcopy(metadata)
        forged["noenterprise_oidc"]["claims"]["roles"] = ["access", "admin"]
        update = httpx.post(
            BASE + "/user/update", headers=headers, json={"user_id": user_id, "metadata": forged}, trust_env=False
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(self.browser.get("/oidc/session").status_code, 401)

    def test_replaying_old_metadata_does_not_restore_revoked_session(self):
        httpx.post(
            ISSUER + "/control/actor",
            json={"sub": "alice", "oid": "stable-alice", "roles": ["access", "operator"]},
            trust_env=False,
        )
        self.assertEqual(self.login().status_code, 303)
        user_id, headers, metadata = self.native_identity_key()
        httpx.post(
            ISSUER + "/control/actor", json={"sub": "alice", "oid": "stable-alice", "roles": []}, trust_env=False
        )
        self.assertEqual(self.login().status_code, 403)
        update = httpx.post(
            BASE + "/user/update", headers=headers, json={"user_id": user_id, "metadata": metadata}, trust_env=False
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(self.browser.get("/oidc/session").status_code, 401)

    def test_another_user_cannot_reuse_a_mcp_session(self):
        httpx.post(
            ISSUER + "/control/actor",
            json={"sub": "alice", "oid": "stable-alice", "roles": ["access", "admin"]},
            trust_env=False,
        )
        self.assertEqual(self.login().status_code, 303)
        headers = {
            "Authorization": "Bearer oidc-browser-session",
            "Origin": BASE,
            "Accept": "application/json, text/event-stream",
        }
        initialized = self.browser.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "identity-test", "version": "1"},
                },
            },
        )
        self.assertEqual(initialized.status_code, 200, initialized.text)
        session_id = initialized.headers["mcp-session-id"]
        httpx.post(
            ISSUER + "/control/actor",
            json={"sub": "bob", "oid": "stable-bob", "email": "shared@example.test", "roles": ["access", "admin"]},
            trust_env=False,
        )
        other = httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False)
        self.addCleanup(other.close)
        self.browser = other
        self.assertEqual(self.login().status_code, 303)
        reused = other.post(
            "/mcp/",
            headers={**headers, "Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-06-18"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(reused.status_code, 404, reused.text)
        self.assertEqual(reused.json()["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
