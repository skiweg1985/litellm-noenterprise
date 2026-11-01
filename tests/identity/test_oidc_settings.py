"""Independent OIDC management through public HTTP and the controlled issuer."""
import copy
import os
import unittest

import httpx

BASE = "http://proxy:4000"
MASTER = os.environ.get("EXPECTED_MASTER_KEY", "sk-identity-test-only")


@unittest.skipUnless(os.environ.get("OIDC_SETTINGS_MODE") == "true", "requires database-owned OIDC fixture")
class OIDCSettingsTests(unittest.TestCase):
    def test_00_unset_configuration_is_admin_only_and_has_no_secrets(self):
        response = httpx.get(BASE + "/oidc/settings", headers={"Authorization": "Bearer " + MASTER}, trust_env=False)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "unset")
        self.assertFalse(body["enabled"])
        self.assertFalse(body["client_secret"]["configured"])
        self.assertEqual(body["revision"], 0)
        self.assertNotIn("fixture-only", response.text)
        self.assertIn(httpx.get(BASE + "/oidc/settings", trust_env=False).status_code, (401, 403))

    def test_save_enables_real_login_and_provider_api_without_exposing_secrets(self):
        admin = {"Authorization": "Bearer " + MASTER}
        current = httpx.get(BASE + "/oidc/settings", headers=admin, trust_env=False).json()
        browser = {"issuer": "http://issuer:8080", "client_id": "litellm-test", "redirect_uri": BASE + "/oidc/callback", "allow_insecure_http": True, "subject_claim": "oid", "access_rule": {"claim": "roles", "any_of": ["access"]}, "role_mappings": [{"rule": {"claim": "roles", "any_of": ["admin"]}, "role": "proxy_admin"}]}
        api = {"resource": BASE, "audience": "litellm-api", "scope_claim": "scp", "token_types": ["JWT"], "scopes": [{"name": "api://fixture/access", "token_value": "access"}]}
        saved = httpx.put(BASE + "/oidc/settings", headers=admin, json={"expected_revision": current["revision"], "browser": browser, "api": api, "client_secret": "fixture-only"}, trust_env=False)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["enabled"])
        self.assertEqual(saved.json()["source"], "database")
        self.assertNotIn("fixture-only", saved.text)
        self.assertTrue(saved.json()["client_secret"]["configured"])
        self.assertTrue(saved.json()["session_secret"]["configured"])
        with httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False) as client:
            start = client.get("/sso/key/generate")
            self.assertEqual(start.status_code, 302, start.text)
            authorized = client.get(start.headers["location"])
            complete = client.get(authorized.headers["location"])
            self.assertEqual(complete.status_code, 303, complete.text)
            self.assertEqual(client.get("/oidc/session").status_code, 200)
        token = httpx.post("http://issuer:8080/control/access-token", json={}, trust_env=False).json()["access_token"]
        accepted = httpx.get(BASE + "/v1/models", headers={"Authorization": "Bearer " + token}, trust_env=False)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        ui = httpx.get(BASE + "/.well-known/litellm-ui-config", trust_env=False)
        self.assertTrue(ui.json()["sso_configured"], ui.text)
        metadata = httpx.get(BASE + "/.well-known/oauth-protected-resource", trust_env=False).json()
        self.assertEqual(metadata["scopes_supported"], ["api://fixture/access"])
