"""Configuration changes must alter both discovery and token enforcement."""

import os
import unittest

import httpx


class ScopeProfileTests(unittest.TestCase):
    def test_configured_profile_matches_metadata_and_admission(self):
        base = "http://proxy:4000"
        advertised = os.environ["EXPECTED_API_SCOPE"]
        token_scope = os.environ["EXPECTED_TOKEN_SCOPE"]
        token_type = os.environ["EXPECTED_TOKEN_TYPE"]
        claims = {"aud": os.environ["EXPECTED_API_AUDIENCE"], "_typ": token_type, "scp": token_scope}
        with httpx.Client(base_url=base, trust_env=False) as client:
            native = client.get(
                "/models", headers={"Authorization": "Bearer " + os.environ["EXPECTED_MASTER_KEY"]}
            )
            self.assertEqual(native.status_code, 200, native.text)
            metadata = client.get("/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(metadata.status_code, 200, metadata.text)
            self.assertEqual(metadata.json()["scopes_supported"], [advertised])
            for overrides, status in (
                ({}, 200),
                ({"aud": [os.environ["EXPECTED_API_AUDIENCE"], "https://provider.example/userinfo"]}, 200),
                ({"scp": "unrelated"}, 403),
                ({"nonce": "id-token"}, 401),
                ({"_typ": "id+jwt"}, 401),
            ):
                token = httpx.post(
                    "http://issuer:8080/control/access-token", json={**claims, **overrides}, trust_env=False
                ).json()["access_token"]
                response = client.get("/models", headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, status, response.text)
            if token_type == "JWT":
                token = httpx.post(
                    "http://issuer:8080/control/access-token",
                    json={**claims, "aud": [os.environ["EXPECTED_API_AUDIENCE"], "litellm-test"]},
                    trust_env=False,
                ).json()["access_token"]
                response = client.get("/models", headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, 401, response.text)
            if token_type == "at+jwt":
                token = httpx.post(
                    "http://issuer:8080/control/access-token", json={**claims, "_typ": "JWT"}, trust_env=False
                ).json()["access_token"]
                response = client.get("/models", headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, 401, response.text)
