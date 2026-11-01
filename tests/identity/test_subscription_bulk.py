"""Review and apply subscription assignments through real admin HTTP endpoints."""

import unittest

import test_subscription_models as models


class SubscriptionBulkTests(models.SubscriptionModelTests):
    def setUp(self):
        super().setUp()
        self.control(models_by_identity={}, status_by_identity={}, catalog_delay=0)

    def preview(self, account):
        response = self.client.get(
            "/chatgpt/api/accounts/" + account["account"] + "/model-assignments"
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, account, rows):
        return self.client.post(
            "/chatgpt/api/accounts/" + account["account"] + "/model-assignments",
            json={"models": rows},
        )

    def test_catalog_permissions_and_configuration_models_are_revalidated(self):
        import httpx

        original, _ = self.connect("CatalogOriginal")
        added, identity = self.connect("CatalogAdded")
        _, created = self.create(original)
        identifier = created.json()["model_id"]
        self.client.patch(
            "/model/" + identifier + "/update",
            json={"model_name": "catalog-public-name"},
        ).raise_for_status()
        plan = self.preview(added)
        row = next(row for row in plan["models"] if row["model_id"] == identifier)
        configured = next(
            row
            for row in plan["models"]
            if row["model_name"] == "fixture-subscription-config"
        )
        self.assertFalse(configured["eligible"])
        self.assertIn("Configuration-backed", configured["reason"])
        rejected = self.apply(
            added,
            [
                {
                    "model_id": configured["model_id"],
                    "version": "unknown",
                    "action": "replace",
                }
            ],
        )
        self.assertEqual(rejected.json()["results"][0]["status"], "conflict")
        # Matching a public name does not make a different upstream slug compatible.
        self.control(models_by_identity={identity: ["catalog-public-name"]})
        incompatible = next(
            row
            for row in self.preview(added)["models"]
            if row["model_id"] == identifier
        )
        self.assertFalse(incompatible["eligible"])
        self.assertEqual(incompatible["upstream_model"], "gpt-5.4")
        self.control(catalog_error=True, models_by_identity={})
        unavailable = self.client.get(
            "/chatgpt/api/accounts/" + added["account"] + "/model-assignments"
        )
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.control(catalog_error=False)
        token = httpx.post(
            "http://issuer:8080/control/access-token",
            json={"oid": "bulk-provider-admin", "roles": ["access", "admin"]},
            trust_env=False,
        ).json()["access_token"]
        for method in ("GET", "POST"):
            response = self.client.request(
                method,
                "/chatgpt/api/accounts/" + added["account"] + "/model-assignments",
                headers={"Authorization": "Bearer " + token},
                json={
                    "models": [
                        {
                            "model_id": identifier,
                            "version": row["version"],
                            "action": "replace",
                        }
                    ]
                }
                if method == "POST"
                else None,
            )
            self.assertEqual(response.status_code, 403, response.text)
        self.client.post(
            "/chatgpt/api/accounts/" + added["account"] + "/logout"
        ).raise_for_status()
        rejected = self.apply(
            added,
            [{"model_id": identifier, "version": row["version"], "action": "replace"}],
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)

    def test_fixed_actions_are_explicit_and_reserve_mode_cannot_be_replaced(self):
        original, _ = self.connect("ExplicitOriginal")
        added, identity = self.connect("ExplicitAdded")
        name, created = self.create(original)
        identifier = created.json()["model_id"]
        row = next(
            row
            for row in self.preview(added)["models"]
            if row["model_id"] == identifier
        )
        choice = {"model_id": identifier, "version": row["version"], "action": "append"}
        self.assertEqual(
            self.apply(added, [choice]).json()["results"][0]["status"], "error"
        )
        choice["action"] = "replace"
        self.assertEqual(
            self.apply(added, [choice]).json()["results"][0]["status"], "saved"
        )
        self.control(clear=True)
        self.client.post(
            "/v1/responses", json={"model": name, "input": "Explicit replacement"}
        ).raise_for_status()
        self.assertEqual(self.control()["seen"], [identity])
        another, _ = self.connect("AnotherExplicit")
        self.client.patch(
            "/model/" + identifier + "/update",
            json={
                "model_info": {
                    "subscription_assignment": {
                        "mode": "priority",
                        "subscription_ids": [added["subscription_id"]],
                    }
                }
            },
        ).raise_for_status()
        row = next(
            row
            for row in self.preview(another)["models"]
            if row["model_id"] == identifier
        )
        rejected = self.apply(
            another,
            [{"model_id": identifier, "version": row["version"], "action": "replace"}],
        )
        self.assertEqual(
            rejected.json()["results"][0]["status"], "error", rejected.text
        )

    def test_simultaneous_saves_use_atomic_model_version_check(self):
        from concurrent.futures import ThreadPoolExecutor

        original, _ = self.connect("ConcurrentOriginal")
        added, _ = self.connect("ConcurrentAdded")
        _, created = self.create(original)
        self.assertEqual(created.status_code, 200, created.text)
        identifier = created.json()["model_id"]
        row = next(
            row
            for row in self.preview(added)["models"]
            if row["model_id"] == identifier
        )
        choices = [
            {"model_id": identifier, "version": row["version"], "action": "priority"}
        ]
        # External catalog latency overlaps the native validation/write window.
        self.control(catalog_delay=0.3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.apply(added, choices), range(2)))
        self.control(catalog_delay=0)
        self.assertEqual(
            sorted(r.json()["results"][0]["status"] for r in responses),
            ["conflict", "saved"],
            [r.text for r in responses],
        )
        stored = next(
            row
            for row in self.preview(added)["models"]
            if row["model_id"] == identifier
        )
        self.assertEqual(
            stored["current"]["subscription_ids"],
            [original["subscription_id"], added["subscription_id"]],
        )
        self.assertFalse(stored["eligible"])
        self.assertIn("already assigned", stored["reason"])

    def test_stale_model_is_a_per_row_conflict_and_retry_cannot_duplicate_members(self):
        original, _ = self.connect("BeforeReview")
        added, _ = self.connect("AfterReview")
        created = []
        for _ in range(2):
            response = self.create(original)[1]
            self.assertEqual(response.status_code, 200, response.text)
            created.append(response.json()["model_id"])
        for identifier in created:
            self.client.patch(
                "/model/" + identifier + "/update",
                json={
                    "model_info": {
                        "subscription_assignment": {
                            "mode": "distribution",
                            "subscription_ids": [original["subscription_id"]],
                        }
                    }
                },
            ).raise_for_status()
        plan = self.preview(added)
        choices = [
            {"model_id": row["model_id"], "version": row["version"], "action": "append"}
            for row in plan["models"]
            if row["model_id"] in created
        ]
        # An unrelated setting is still part of the reviewed model version.
        self.client.patch(
            "/model/" + created[0] + "/update",
            json={"litellm_params": {"temperature": 0.7}},
        ).raise_for_status()
        saved = self.apply(added, choices)
        self.assertEqual(saved.status_code, 200, saved.text)
        results = {row["model_id"]: row for row in saved.json()["results"]}
        self.assertEqual(results[created[0]]["status"], "conflict", saved.text)
        self.assertEqual(results[created[1]]["status"], "saved", saved.text)
        retry = self.apply(added, choices)
        self.assertTrue(
            all(row["status"] == "conflict" for row in retry.json()["results"]),
            retry.text,
        )
        visible = self.client.get("/v1/model/info").json()["data"]
        for row in visible:
            if row["model_info"]["id"] in created:
                expected = (
                    [original["subscription_id"]]
                    if row["model_info"]["id"] == created[0]
                    else [original["subscription_id"], added["subscription_id"]]
                )
                self.assertEqual(
                    row["model_info"]["subscription_assignment"]["subscription_ids"],
                    expected,
                )
        self.assertTrue(
            next(
                a
                for a in self.client.get("/chatgpt/api/accounts").json()["accounts"]
                if a["subscription_id"] == added["subscription_id"]
            )["token_valid"]
        )
        refreshed = next(
            row
            for row in self.preview(added)["models"]
            if row["model_id"] == created[0]
        )
        result = self.apply(
            added,
            [
                {
                    "model_id": created[0],
                    "version": refreshed["version"],
                    "action": "append",
                }
            ],
        )
        self.assertEqual(result.json()["results"][0]["status"], "saved", result.text)
        row = next(
            row
            for row in self.client.get("/v1/model/info").json()["data"]
            if row["model_info"]["id"] == created[0]
        )
        self.assertEqual(row["litellm_params"]["temperature"], 0.7)
        self.assertEqual(
            row["model_info"]["subscription_assignment"]["subscription_ids"],
            [original["subscription_id"], added["subscription_id"]],
        )

    def test_review_appends_selected_reserve_and_keeps_fixed_model_unchanged(self):
        first, first_identity = self.connect("Original")
        added, added_identity = self.connect("New")
        name, created = self.create(first)
        reserve_id = created.json()["model_id"]
        self.client.patch(
            "/model/" + reserve_id + "/update",
            json={
                "litellm_params": {"temperature": 0.3},
                "model_info": {
                    "subscription_assignment": {
                        "mode": "priority",
                        "subscription_ids": [first["subscription_id"]],
                    }
                },
            },
        ).raise_for_status()
        fixed_name, fixed = self.create(first)
        plan = self.preview(added)
        reserve = next(row for row in plan["models"] if row["model_id"] == reserve_id)
        fixed_row = next(
            row for row in plan["models"] if row["model_id"] == fixed.json()["model_id"]
        )
        self.assertEqual(
            reserve["proposed"]["subscription_ids"],
            [first["subscription_id"], added["subscription_id"]],
        )
        self.assertTrue(reserve["selected"])
        self.assertFalse(fixed_row["selected"])
        self.assertIsNone(fixed_row["proposed"])
        result = self.apply(
            added,
            [
                {
                    "model_id": reserve_id,
                    "version": reserve["version"],
                    "action": "append",
                }
            ],
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["results"][0]["status"], "saved")
        stored = self.client.get("/v1/model/info").json()["data"]
        row = next(row for row in stored if row["model_info"]["id"] == reserve_id)
        self.assertEqual(row["litellm_params"]["temperature"], 0.3)
        self.assertEqual(
            row["model_info"]["subscription_assignment"]["subscription_ids"],
            [first["subscription_id"], added["subscription_id"]],
        )
        self.control(status_by_identity={first_identity: 503}, clear=True)
        response = self.client.post(
            "/v1/responses", json={"model": name, "input": "Use reviewed reserve"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.control()["seen"], [first_identity, added_identity])
        fixed_row = next(row for row in stored if row["model_name"] == fixed_name)
        self.assertEqual(
            fixed_row["model_info"]["subscription_assignment"]["subscription_ids"],
            [first["subscription_id"]],
        )


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        SubscriptionBulkTests(name)
        for name in SubscriptionBulkTests.__dict__
        if name.startswith("test_")
    )
