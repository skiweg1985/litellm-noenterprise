"""Account linking through public admin, login and provider-token interfaces."""

import concurrent.futures
import json
import os
from pathlib import Path
import time
import unittest
import uuid
import httpx

BASE = "http://proxy:4000"
ISSUER = "http://issuer:8080"
MASTER = {"Authorization": "Bearer sk-identity-test-only"}


class LinkingTests(unittest.TestCase):
    def setUp(self):
        self.client = httpx.Client(
            base_url=BASE, headers=MASTER, trust_env=False, timeout=30
        )
        self.addCleanup(self.client.close)
        self.subject = "link-" + uuid.uuid4().hex
        self.email = self.subject + "@example.test"
        self.browser = httpx.Client(
            base_url=BASE, trust_env=False, follow_redirects=False, timeout=30
        )
        self.addCleanup(self.browser.close)

    def native(self):
        result = self.client.post(
            "/user/new",
            json={
                "user_id": "native-" + self.subject,
                "user_email": self.email,
                "user_role": "internal_user",
                "max_budget": 42,
                "models": ["fixture-model"],
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def login(self, **overrides):
        claims = {
            "sub": self.subject,
            "oid": self.subject,
            "email": self.email,
            "roles": ["access"],
        }
        claims.update(overrides)
        httpx.post(
            ISSUER + "/control/actor", json=claims, trust_env=False
        ).raise_for_status()
        start = self.browser.get("/oidc/login")
        auth = self.browser.get(start.headers["location"])
        return self.browser.get(auth.headers["location"])

    def inventory(self):
        response = self.client.get("/oidc/links")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def record(self):
        return next(
            row for row in self.inventory()["links"] if row["subject"] == self.subject
        )

    def action(self, record, action, **values):
        return self.client.post(
            "/oidc/links/" + record["identity_key"] + "/" + action,
            json={"expected_revision": record["revision"], **values},
        )

    def test_existing_user_waits_for_admin_link_and_keeps_native_identity(self):
        native = self.native()
        pending = self.login()
        self.assertEqual(pending.status_code, 403, pending.text)
        self.assertIn("administrator review", pending.text)
        self.assertEqual(self.browser.get("/oidc/session").status_code, 401)
        record = self.record()
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["candidates"], [native["user_id"]])
        approved = self.action(record, "approve", user_id=native["user_id"])
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(
            self.browser.get("/oidc/session").json()["user_id"], native["user_id"]
        )
        info = self.client.get(
            "/user/info", params={"user_id": native["user_id"]}
        ).json()["user_info"]
        self.assertEqual(info["max_budget"], 42)
        self.assertEqual(info["models"], ["fixture-model"])
        self.assertEqual(info["user_role"], "internal_user_viewer")
        self.assertEqual(
            self.client.get("/key/info", params={"key": native["key"]}).status_code, 200
        )

    def token(self, **overrides):
        claims = {"oid": self.subject, "sub": self.subject, "email": self.email}
        claims.update(overrides)
        response = httpx.post(
            ISSUER + "/control/access-token", json=claims, trust_env=False
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def test_unlink_revokes_browser_and_api_without_recreating_derived_account(self):
        self.assertEqual(self.login().status_code, 303)
        record = self.record()
        user_id = record["user_id"]
        token = self.token()
        self.assertEqual(self.client.get("/models", headers=token).status_code, 200)
        result = self.action(record, "unlink")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.browser.get("/oidc/session").status_code, 401)
        self.assertEqual(self.client.get("/models", headers=token).status_code, 403)
        self.assertEqual(self.login(email="changed-" + self.email).status_code, 403)
        self.assertEqual(
            self.client.get("/user/info", params={"user_id": user_id}).status_code, 200
        )
        rejected = self.record()
        self.assertEqual(rejected["state"], "unlinked")
        reopened = self.action(rejected, "reopen")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        approved = self.action(reopened.json(), "approve", user_id=user_id)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.browser.get("/oidc/session").json()["user_id"], user_id)

    def test_api_pending_and_rejection_cannot_be_bypassed_by_changing_email(self):
        native = self.native()
        response = self.client.get("/models", headers=self.token())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("linking_required", response.text)
        record = self.record()
        rejected = self.action(record, "reject")
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(
            self.client.get("/models", headers=self.token(email=None)).status_code, 403
        )
        self.assertEqual(self.login(email="different-" + self.email).status_code, 403)
        record = self.record()
        self.assertEqual(record["state"], "rejected")
        self.assertEqual(
            self.action(record, "approve", user_id=native["user_id"]).status_code, 409
        )
        reopened = self.action(record, "reopen")
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(
            self.action(
                reopened.json(), "approve", user_id=native["user_id"]
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/models", headers=self.token()).status_code, 200
        )
        info = self.client.get(
            "/user/info", params={"user_id": native["user_id"]}
        ).json()["user_info"]
        self.assertEqual(
            info["user_role"],
            "internal_user",
            "API grant must not overwrite persisted browser role",
        )

    def test_concurrent_approval_is_idempotent_and_conflicts_do_not_change_target(self):
        native = self.native()
        self.assertEqual(self.login().status_code, 403)
        record = self.record()

        def approve():
            return httpx.post(
                BASE + "/oidc/links/" + record["identity_key"] + "/approve",
                headers=MASTER,
                json={
                    "expected_revision": record["revision"],
                    "user_id": native["user_id"],
                },
                trust_env=False,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: approve(), range(4)))
        self.assertEqual(
            [r.status_code for r in responses], [200] * 4, [r.text for r in responses]
        )
        self.assertEqual(len({r.json()["revision"] for r in responses}), 1)
        self.assertEqual(
            self.action(record, "approve", user_id="other-user").status_code, 409
        )
        self.assertEqual(self.action(record, "reject").status_code, 409)
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(
            self.browser.get("/oidc/session").json()["user_id"], native["user_id"]
        )

    def test_native_writes_and_non_admins_cannot_forge_or_change_links(self):
        native = self.native()
        self.assertEqual(self.login().status_code, 403)
        pending = self.record()
        forged = self.client.post(
            "/user/update",
            json={"user_id": native["user_id"], "sso_user_id": pending["identity_key"]},
        )
        self.assertIn(forged.status_code, (200, 400, 409), forged.text)
        self.assertEqual(self.record()["state"], "pending")
        forged = self.client.post(
            "/user/update",
            json={
                "user_id": native["user_id"],
                "metadata": {"noenterprise_oidc": {"active": True}},
            },
        )
        self.assertIn(forged.status_code, (400, 409), forged.text)
        for headers in (
            {"Authorization": "Bearer " + native["key"]},
            self.token(roles=["access", "admin"]),
        ):
            response = self.client.post(
                "/oidc/links/" + pending["identity_key"] + "/approve",
                headers=headers,
                json={
                    "expected_revision": pending["revision"],
                    "user_id": native["user_id"],
                },
            )
            self.assertIn(response.status_code, (401, 403), response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            self.action(
                self.record(), "approve", user_id=native["user_id"]
            ).status_code,
            200,
        )
        response = self.client.post(
            "/user/update", json={"user_id": native["user_id"], "sso_user_id": None}
        )
        self.assertIn(response.status_code, (200, 400, 409), response.text)
        self.assertEqual(self.record()["state"], "linked")
        self.assertEqual(self.login().status_code, 303)

    def test_historical_api_identity_requires_explicit_confirmation_without_overwrite(
        self,
    ):
        self.subject = "historical-api"
        self.email = self.subject + "@example.test"
        before = next(
            u for u in self.inventory()["users"] if u["user_email"] == self.email
        )
        self.assertEqual(before["sso_status"], "legacy")
        historical = self.client.get(
            "/spend/logs/ui", params={"request_id": "historical-request"}
        ).json()["data"]
        self.assertEqual(historical[0]["user"], before["user_id"])
        self.assertEqual(historical[0]["spend"], 1.25)

        self.assertEqual(
            self.client.get("/models", headers=self.token()).status_code, 403
        )
        record = self.record()
        user = next(
            u for u in self.inventory()["users"] if u["user_id"] == before["user_id"]
        )
        self.assertEqual(user["sso_status"], "unconfirmed_oidc")
        self.assertEqual(
            self.action(record, "approve", user_id=user["user_id"]).status_code, 200
        )
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(
            self.browser.get("/oidc/session").json()["user_id"], user["user_id"]
        )
        info = self.client.get(
            "/user/info", params={"user_id": user["user_id"]}
        ).json()["user_info"]
        self.assertEqual(info["organization_id"], "historical-org")
        self.assertEqual(info["teams"], ["historical-team"])
        self.assertEqual(info["max_budget"], 42)
        self.assertEqual(info["spend"], 1.25)
        self.assertEqual(
            self.client.get(
                "/models",
                headers={"Authorization": "Bearer sk-historical-key-fixture-only"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/spend/logs/ui", params={"request_id": "historical-request"}
            ).json()["data"],
            historical,
        )

    def test_historical_signed_browser_identity_remains_the_same_account(self):
        self.subject = "historical-browser"
        self.email = self.subject + "@example.test"
        before = next(
            u for u in self.inventory()["users"] if u["user_email"] == self.email
        )
        self.assertEqual(before["sso_status"], "independent_oidc")
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(
            self.browser.get("/oidc/session").json()["user_id"], before["user_id"]
        )

    def test_legacy_link_conflicts_and_ambiguous_email_never_select_an_account(self):
        self.email = "historical-legacy@example.test"
        self.assertEqual(self.login().status_code, 403)
        record = self.record()
        self.assertEqual(
            self.action(record, "approve", user_id="historical-legacy").status_code, 409
        )
        self.email = "ambiguous@example.test"
        self.assertEqual(self.login().status_code, 403)
        record = self.record()
        self.assertEqual(set(record["candidates"]), {"ambiguous-a", "ambiguous-b"})
        self.assertEqual(
            self.action(record, "approve", user_id="ambiguous-b").status_code, 200
        )
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(
            self.browser.get("/oidc/session").json()["user_id"], "ambiguous-b"
        )

    def test_z_associations_and_pending_decisions_survive_restart(self):
        artifact = Path("/restart/oidc-linking.json")
        if os.environ.get("OIDC_LINKING_RESTART_PHASE") == "read":
            saved = json.loads(artifact.read_text())
            self.subject, self.email = saved["subject"], saved["email"]
            self.browser.cookies.update(saved["cookies"])
            self.assertEqual(
                self.browser.get("/oidc/session").json()["user_id"], saved["user_id"]
            )
            record = self.record()
            self.assertEqual(record["state"], "linked")
            self.assertEqual(record["user_id"], saved["user_id"])
            states = {r["identity_key"]: r["state"] for r in self.inventory()["links"]}
            self.assertEqual(states[saved["pending_key"]], "pending")
            self.assertEqual(
                self.client.get("/models", headers=self.token()).status_code, 200
            )
            return
        native = self.native()
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(
            self.action(
                self.record(), "approve", user_id=native["user_id"]
            ).status_code,
            200,
        )
        self.assertEqual(self.login().status_code, 303)
        pending_token = self.token(
            oid=self.subject + "-other", sub=self.subject + "-other"
        )
        self.assertEqual(
            self.client.get("/models", headers=pending_token).status_code, 403
        )
        pending = next(
            r
            for r in self.inventory()["links"]
            if r["subject"] == self.subject + "-other"
        )
        artifact.write_text(
            json.dumps(
                {
                    "subject": self.subject,
                    "email": self.email,
                    "user_id": native["user_id"],
                    "cookies": dict(self.browser.cookies),
                    "pending_key": pending["identity_key"],
                }
            )
        )

    def test_personal_mcp_connection_survives_unlink_and_explicit_relink(self):
        from test_mcp import PersonalMCPTests

        native = self.native()
        roles = ["access", "mcp-a"]
        self.assertEqual(self.login(roles=roles).status_code, 403)
        self.assertEqual(
            self.action(
                self.record(), "approve", user_id=native["user_id"]
            ).status_code,
            200,
        )
        headers = self.token(roles=roles)
        code, verifier = PersonalMCPTests.connect(
            self, headers, actor="preserved-personal"
        )
        response = PersonalMCPTests.exchange(self, headers, code, verifier)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["has_credential"])
        self.assertEqual(self.action(self.record(), "unlink").status_code, 200)
        path = "/v1/mcp/server/managed_a/oauth-user-credential/status"
        self.assertEqual(self.client.get(path, headers=headers).status_code, 403)
        reopened = self.action(self.record(), "reopen")
        self.assertEqual(
            self.action(
                reopened.json(), "approve", user_id=native["user_id"]
            ).status_code,
            200,
        )
        status = self.client.get(path, headers=headers)
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["has_credential"])
        call = self.client.post(
            "/mcp-rest/tools/call",
            headers=headers,
            json={"server_id": "managed_a", "name": "whoami", "arguments": {}},
        )
        self.assertEqual(call.status_code, 200, call.text)
        self.assertIn("preserved-personal", call.text)

    def test_user_id_prefix_does_not_revoke_another_users_sessions(self):
        first = self.native()
        first_subject, first_email = self.subject, self.email
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(
            self.action(self.record(), "approve", user_id=first["user_id"]).status_code,
            200,
        )
        self.assertEqual(self.login().status_code, 303)
        first_browser = self.browser
        self.subject += "-ops"
        self.email = self.subject + "@example.test"
        second = self.client.post(
            "/user/new",
            json={"user_id": first["user_id"] + ":ops", "user_email": self.email},
        ).json()
        self.browser = httpx.Client(
            base_url=BASE, trust_env=False, follow_redirects=False
        )
        self.addCleanup(self.browser.close)
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(
            self.action(
                self.record(), "approve", user_id=second["user_id"]
            ).status_code,
            200,
        )
        self.assertEqual(self.login().status_code, 303)
        second_browser = self.browser
        self.subject, self.email, self.browser = (
            first_subject,
            first_email,
            first_browser,
        )
        self.assertEqual(self.login(roles=["access", "admin"]).status_code, 303)
        self.assertEqual(second_browser.get("/oidc/session").status_code, 200)
        self.assertEqual(self.action(self.record(), "unlink").status_code, 200)
        self.assertEqual(first_browser.get("/oidc/session").status_code, 401)
        self.assertEqual(second_browser.get("/oidc/session").status_code, 200)

    def test_late_callback_cannot_regain_access_after_unlink_and_other_identity_link(
        self,
    ):
        native = self.native()
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(
            self.action(
                self.record(), "approve", user_id=native["user_id"]
            ).status_code,
            200,
        )
        self.assertEqual(self.login().status_code, 303)
        old_record = self.record()
        new_subject = self.subject + "-replacement"
        self.assertEqual(
            self.client.get(
                "/models", headers=self.token(oid=new_subject, sub=new_subject)
            ).status_code,
            403,
        )
        replacement = next(
            r for r in self.inventory()["links"] if r["subject"] == new_subject
        )
        gate = httpx.Client(
            base_url="http://linking-gate:8093", trust_env=False, timeout=10
        )
        self.addCleanup(gate.close)
        gate.post("/arm", json={"user_id": native["user_id"]}).raise_for_status()
        self.addCleanup(lambda: gate.post("/release"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            callback = pool.submit(self.login)
            deadline = time.monotonic() + 12
            while not gate.get("/status").json()["insert_waiting"]:
                if callback.done():
                    self.fail(
                        "Callback did not reach the controlled session-insert boundary: "
                        + callback.result().text
                    )
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "Callback did not reach the fixture gate",
                )
                time.sleep(0.05)
            unlink = pool.submit(self.action, old_record, "unlink")
            # Observe the database boundary, not an arbitrary scheduling sleep. The
            # fixed transaction waits for the callback; the old version finishes
            # unlink before the invisible session insert is committed.
            while not unlink.done() and not gate.get("/status").json()["other_waiting"]:
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "Unlink did not reach its transaction boundary",
                )
                time.sleep(0.05)
            if unlink.done():
                self.assertEqual(unlink.result().status_code, 200)
                self.assertEqual(
                    self.action(
                        replacement, "approve", user_id=native["user_id"]
                    ).status_code,
                    200,
                )
                gate.post("/release").raise_for_status()
            else:
                gate.post("/release").raise_for_status()
                self.assertEqual(unlink.result(timeout=15).status_code, 200)
                self.assertEqual(
                    self.action(
                        replacement, "approve", user_id=native["user_id"]
                    ).status_code,
                    200,
                )
            self.assertIn(callback.result(timeout=15).status_code, (303, 403))
        self.assertEqual(self.browser.get("/oidc/session").status_code, 401)
        self.subject = new_subject
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(
            self.browser.get("/oidc/session").json()["user_id"], native["user_id"]
        )
