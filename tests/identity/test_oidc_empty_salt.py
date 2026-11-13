"""Rejected configuration saves must not persist unusable encrypted credentials."""

import os
import unittest

import httpx


@unittest.skipUnless(
    os.environ.get("OIDC_EMPTY_SALT_MODE") == "true",
    "requires the isolated empty-encryption-key fixture",
)
class EmptyEncryptionKeyTests(unittest.TestCase):
    def test_save_rejects_empty_effective_key_without_changing_configuration(self):
        with httpx.Client(
            base_url="http://proxy:4000",
            headers={
                "Authorization": "Bearer "
                + os.environ.get("EXPECTED_MASTER_KEY", "sk-identity-test-only")
            },
            trust_env=False,
        ) as client:
            before = client.get("/oidc/settings")
            self.assertEqual(before.status_code, 200, before.text)
            self.assertEqual(before.json()["revision"], 0)
            self.assertEqual(before.json()["source"], "unset")
            saved = client.put(
                "/oidc/settings",
                json={
                    "expected_revision": 0,
                    "browser": {
                        "issuer": "http://issuer:8080",
                        "client_id": "litellm-test",
                        "redirect_uri": "http://proxy:4000/oidc/callback",
                        "allow_insecure_http": True,
                        "access_rule": {"claim": "roles", "any_of": ["access"]},
                    },
                    "api": None,
                    "client_secret": "empty-key-save-secret-sentinel",
                },
            )
            self.assertEqual(saved.status_code, 503, saved.text)
            self.assertEqual(saved.json()["detail"]["code"], "prerequisite_missing")
            self.assertEqual(saved.headers.get("cache-control"), "no-store")
            self.assertNotIn("empty-key-save-secret-sentinel", saved.text)
            after = client.get("/oidc/settings")
            self.assertEqual(after.status_code, 200, after.text)
            self.assertEqual(after.json(), before.json())
            recovery = client.get("/user/list")
            self.assertEqual(recovery.status_code, 200, recovery.text)
