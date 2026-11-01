"""Subscription administration boundaries through the installed public proxy."""

import base64
import json
import unittest

import httpx

BASE = "http://proxy:4000"
ISSUER = "http://issuer:8080"
API = "/chatgpt/api/accounts"
BROWSER_AUTH = {"Authorization": "Bearer oidc-browser-session"}


class SubscriptionAdministrationTests(unittest.TestCase):
    def client(self):
        client = httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False)
        self.addCleanup(client.close)
        return client

    def browser(self, roles):
        client = self.client()
        httpx.post(
            ISSUER + "/control/actor",
            json={"sub": "subscription-browser", "oid": "subscription-" + roles[-1], "roles": roles},
            trust_env=False,
        ).raise_for_status()
        start = client.get("/oidc/login")
        authorization = client.get(start.headers["location"])
        callback = client.get(authorization.headers["location"])
        self.assertEqual(callback.status_code, 303)
        return client

    def test_oidc_admin_reads_and_mutates_only_from_its_origin(self):
        client = self.browser(["access", "admin"])
        result = client.get(API, headers=BROWSER_AUTH)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["accounts"][0]["account"], "default")
        self.assertEqual(result.headers["cache-control"], "no-store")
        # Cancellation exercises a real mutation without starting provider authorization.
        for origin in (None, "https://other.invalid"):
            headers = dict(BROWSER_AUTH)
            if origin:
                headers["Origin"] = origin
            denied = client.post(API + "/default/login/cancel", headers=headers)
            self.assertEqual(denied.status_code, 403, denied.text)
        allowed = client.post(
            API + "/default/login/cancel", headers={**BROWSER_AUTH, "Origin": BASE}
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_personal_browser_and_provider_grants_cannot_administer_subscriptions(self):
        client = self.browser(["access"])
        self.assertEqual(client.get(API, headers=BROWSER_AUTH).status_code, 403)
        result = httpx.post(
            ISSUER + "/control/access-token",
            json={"oid": "subscription-api-admin", "roles": ["access", "admin"]},
            trust_env=False,
        )
        result.raise_for_status()
        token = result.json()["access_token"]
        self.assertEqual(
            self.client().get(API, headers={"Authorization": "Bearer " + token}).status_code, 403
        )

    def test_native_dashboard_login_key_can_read_subscriptions(self):
        client = self.client()
        login = client.post("/login", data={"username": "admin", "password": "sk-identity-test-only"})
        self.assertIn(login.status_code, (302, 303))
        envelope = client.cookies.get("token")
        self.assertIsNotNone(envelope)
        encoded = envelope.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        result = client.get(API, headers={"Authorization": "Bearer " + claims["key"]})
        self.assertEqual(result.status_code, 200, result.text)

    def test_anonymous_and_monitoring_key_do_not_grant_administration(self):
        client = self.client()
        for headers in ({}, {"X-Monitoring-Key": "synthetic-monitoring-key"}):
            self.assertIn(client.get(API, headers=headers).status_code, (401, 403))
        self.assertEqual(client.get("/chatgpt/accounts").status_code, 503)
