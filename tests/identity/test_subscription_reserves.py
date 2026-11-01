"""Preferred subscriptions and reserves through the installed proxy boundary."""

import test_subscription_logs as log_tests
import test_subscription_models as model_tests


class SubscriptionReserveTests(log_tests.SubscriptionLogTests):
    def setUp(self):
        super().setUp()
        self.control(status_by_identity={})

    def create_reserve(self, accounts):
        name, created = self.create(accounts[0])
        self.assertEqual(created.status_code, 200, created.text)
        model_id = created.json()["model_id"]
        updated = self.client.patch(
            "/model/" + model_id + "/update",
            json={
                "model_info": {
                    "subscription_assignment": {
                        "mode": "priority",
                        "subscription_ids": [a["subscription_id"] for a in accounts],
                    }
                }
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        return name, model_id

    def test_outage_moves_to_reserve_and_never_escapes_membership(self):
        first, first_identity = self.connect("OutagePrimary")
        second, second_identity = self.connect("OutageReserve")
        name, model_id = self.create_reserve([first, second])
        self.control(status_by_identity={first_identity: 503}, clear=True)
        result = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "messages": [{"role": "user", "content": "Reserve synthetic"}],
                "num_retries": 9999,
                "fallbacks": [{name: ["fixture-model"]}],
                "disable_fallbacks": False,
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.control()["seen"], [first_identity, second_identity])
        row = self.wait_logs(name)["data"][0]
        attempts = row["metadata"]["subscription_attempts"]
        self.assertEqual(
            [a["subscription_id"] for a in attempts],
            [first["subscription_id"], second["subscription_id"]],
        )
        self.assertEqual([a["outcome"] for a in attempts], ["failure", "success"])
        self.assertEqual([a["reason"] for a in attempts], ["initial", "fallback"])
        self.assertEqual(row["model_id"], model_id)
        self.assertEqual([a["routing_mode"] for a in attempts], ["priority", "priority"])
        self.assertEqual([a["reserve_switch"] for a in attempts], [False, True])
        self.control(clear=True)
        again = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "messages": [{"role": "user", "content": "Still cooling"}],
            },
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self.control()["seen"], [second_identity])

    def test_preferred_subscription_is_used_without_changing_another_model(self):
        first, first_identity = self.connect("Preferred")
        second, second_identity = self.connect("Reserve")
        name, model_id = self.create_reserve([first, second])
        unrelated, created = self.create(second)
        self.assertEqual(created.status_code, 200, created.text)
        for model in (name, unrelated, name):
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "user", "content": "Ordered synthetic " + model}
                    ],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.control()["seen"], [first_identity, second_identity, first_identity]
        )
        rows = self.wait_logs(name, 2)["data"]
        self.assertTrue(
            all(
                row["metadata"]["subscription_ids"] == [first["subscription_id"]]
                for row in rows
            )
        )
        self.assertTrue(all(row["model_id"] == model_id for row in rows))


for _parent in (log_tests.SubscriptionLogTests, model_tests.SubscriptionModelTests):
    for _name in _parent.__dict__:
        if _name.startswith("test_"):
            setattr(SubscriptionReserveTests, _name, None)

del _parent
