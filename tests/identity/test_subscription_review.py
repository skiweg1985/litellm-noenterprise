"""Review regressions through the real admin and inference APIs."""

import concurrent.futures
import unittest

import test_subscription_models as model_tests


class SubscriptionReviewTests(model_tests.SubscriptionModelTests):
    def test_every_assigned_member_reports_affected_models_after_edits(self):
        first, _ = self.connect("ReviewPrimary")
        second, _ = self.connect("ReviewSecondary")
        third, _ = self.connect("ReviewReplacement")
        for mode in ("priority", "distribution"):
            name, created = self.create(first)
            model_id = created.json()["model_id"]

            def assign(members):
                self.client.patch(
                    "/model/" + model_id + "/update",
                    json={
                        "model_info": {
                            "subscription_assignment": {
                                "mode": mode,
                                "subscription_ids": [
                                    a["subscription_id"] for a in members
                                ],
                            }
                        }
                    },
                ).raise_for_status()

            def verify(_):
                rows = {
                    a["subscription_id"]: a
                    for a in self.client.get("/chatgpt/api/accounts").json()["accounts"]
                }
                for member in (first, second):
                    self.assertIn(name, rows[member["subscription_id"]]["models"])
                    self.assertIn(
                        model_id, rows[member["subscription_id"]]["deployment_ids"]
                    )
                self.assertNotIn(name, rows[third["subscription_id"]]["models"])

            assign([first, second])
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(verify, range(8)))
            assign([first, third])
            for _ in range(8):
                rows = {
                    a["subscription_id"]: a
                    for a in self.client.get("/chatgpt/api/accounts").json()["accounts"]
                }
                self.assertIn(name, rows[third["subscription_id"]]["models"])
                self.assertNotIn(name, rows[second["subscription_id"]]["models"])
            self.client.post("/model/delete", json={"id": model_id}).raise_for_status()
            self.models.remove(model_id)
            rows = self.client.get("/chatgpt/api/accounts").json()["accounts"]
            self.assertTrue(all(name not in row["models"] for row in rows))

    def test_model_availability_reports_cooldown_and_model_isolation(self):
        first, first_identity = self.connect("ReviewCoolingFirst")
        second, second_identity = self.connect("ReviewCoolingSecond")
        name, created = self.create(first)
        model_id = created.json()["model_id"]
        self.client.patch(
            "/model/" + model_id + "/update",
            json={
                "model_info": {
                    "cooldown_time": 2,
                    "subscription_assignment": {
                        "mode": "priority",
                        "subscription_ids": [
                            first["subscription_id"],
                            second["subscription_id"],
                        ],
                    },
                }
            },
        ).raise_for_status()
        _, sibling = self.create(first)
        path = "/chatgpt/api/models/" + model_id + "/availability"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "eligible")
        import httpx

        token = httpx.post(
            "http://issuer:8080/control/access-token",
            json={"oid": "availability-provider-admin", "roles": ["access", "admin"]},
            trust_env=False,
        ).json()["access_token"]
        denied = self.client.get(path, headers={"Authorization": "Bearer " + token})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.control(status_by_identity={first_identity: 429, second_identity: 429})
        failed = self.client.post(
            "/v1/responses", json={"model": name, "input": "All limited"}
        )
        self.assertEqual(failed.status_code, 429, failed.text)
        status = self.client.get(path).json()
        self.assertEqual(status["state"], "unavailable", status)
        self.assertEqual(
            [member["state"] for member in status["members"]], ["cooldown", "cooldown"]
        )
        self.assertTrue(all(member["retry_at"] for member in status["members"]))
        other = self.client.get(
            "/chatgpt/api/models/" + sibling.json()["model_id"] + "/availability"
        ).json()
        self.assertEqual(other["state"], "eligible", other)
        import time

        time.sleep(2.2)
        self.assertEqual(self.client.get(path).json()["state"], "eligible")
        self.client.post("/model/block", json={"model_id": model_id}).raise_for_status()
        self.assertEqual(self.client.get(path).json()["state"], "paused")
        self.control(status_by_identity={})

    def test_fixed_model_status_includes_native_credential_cooldown(self):
        account, identity = self.connect("ReviewFixedRejected")
        name, created = self.create(account)
        model_id = created.json()["model_id"]
        self.control(status_by_identity={identity: 401})
        failed = self.client.post(
            "/v1/responses", json={"model": name, "input": "Fixed rejection"}
        )
        self.assertEqual(failed.status_code, 401, failed.text)
        status = self.client.get(
            "/chatgpt/api/models/" + model_id + "/availability"
        ).json()
        self.assertEqual(status["state"], "unavailable", status)
        self.assertEqual(status["members"][0]["state"], "cooldown", status)
        self.control(status_by_identity={})


for _name in tuple(model_tests.SubscriptionModelTests.__dict__):
    if _name.startswith("test_"):
        setattr(SubscriptionReviewTests, _name, None)
